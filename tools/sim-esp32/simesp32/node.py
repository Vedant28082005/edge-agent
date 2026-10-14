"""One virtual ESP32: the firmware state machine from ARCHITECTURE.md 4.3 minus BLE/Wi-Fi/NTP.

All logic runs under one lock; the transport thread calls on_connect/on_disconnect/on_message and the
runner calls tick() about every 100 ms. Time comes from an injectable Clock, so tests never sleep."""
import heapq
import json
import logging
import random
import re
import threading
from collections import OrderedDict

from .clock import Clock
from .cover import Cover
from .generators import Generator
from .schemas import SchemaViolation, Validator

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EP_RE = re.compile(r"^[a-z0-9_]{1,16}$")
PREFIX = "home/v1/nodes/"


class Node:
    def __init__(self, cfg, transport, validator: Validator, clock: Clock | None = None):
        self.cfg = cfg
        self.id = cfg.node_id
        self.t = transport
        self.v = validator
        self.clock = clock or Clock()
        self.log = logging.getLogger(f"sim.{self.id[-6:]}")
        self.lock = threading.RLock()
        self.rng = random.Random(cfg.seed)
        self.faults = cfg.faults
        self.base = f"{PREFIX}{self.id}/"
        transport.node = self

        self.values = {}              # local_id -> cap id -> current value
        self.caps = {}                # local_id -> cap id -> CapConfig
        self.covers = {}              # local_id -> Cover (endpoints with a position capability)
        self.gens = {}                # (local_id, cap id) -> Generator
        self.next_sample = {}         # (local_id, cap id) -> monotonic time
        self.on_since = {}            # (local_id, cap id) -> time a max_on_s capability turned on
        self.next_progress = {}       # local_id -> next in-motion state publish
        self.dedup = OrderedDict()    # cmd_id -> ack dict
        self._sched = []              # heap of (due, seq, fn)
        self._seq = 0

        self.link_up = False
        self.lost_since = None
        self.failsafe_done = False
        self.connected_at = None
        self.provisioned = True
        self.stopped = False
        self.identify_until = None
        self.last_state_pub = float("-inf")
        self._drop_first = self.faults.drop_first_acks

        # observable counters (tests, CLI summary)
        self.stats = {"acks_sent": 0, "acks_dropped": 0, "cmds_applied": 0, "cmds_deduped": 0,
                      "violations": 0, "failsafes": 0, "max_on_trips": 0}
        self.violation_log = []

    # ---------------------------------------------------------------- boot
    def boot(self):
        """LOAD_CONFIG -> apply boot_value, before any network (4.3)."""
        with self.lock:
            now = self.clock.monotonic()
            self.lost_since = now
            for ep in self.cfg.endpoints:
                vals, caps = {}, {}
                for c in ep.caps:
                    caps[c.id] = c
                    if c.kind == "actuator":
                        vals[c.id] = c.boot_value
                    else:
                        g = Generator(c, c.generator, random.Random(f"{self.cfg.seed}/{ep.local_id}/{c.id}"))
                        self.gens[(ep.local_id, c.id)] = g
                        vals[c.id] = g.sample(0.0)
                        self.next_sample[(ep.local_id, c.id)] = now + c.interval_s
                self.values[ep.local_id] = vals
                self.caps[ep.local_id] = caps
                if "position" in caps:
                    lo, hi = caps["position"].effective_range()
                    self.covers[ep.local_id] = Cover(vals["position"], lo, hi, ep.travel_time_s, ep.interlock_s, now)
                    self.next_progress[ep.local_id] = now
                for c in ep.caps:
                    if c.kind == "actuator" and c.max_on_s and vals[c.id] is True:
                        self.on_since[(ep.local_id, c.id)] = now

    # ---------------------------------------------------------------- transport callbacks
    # Callbacks run on the transport's thread. They bail out on `stopped` *before* taking the lock so that
    # shutdown()/unprovision (which hold the lock while joining that thread) cannot deadlock against them.
    def on_connect(self):
        if self.stopped:
            return
        with self.lock:
            if self.stopped:
                return
            now = self.clock.monotonic()
            self.link_up = True
            self.lost_since = None
            self.failsafe_done = False
            self.connected_at = now
            # contracts/mqtt.md "Connect lifecycle": on EVERY successful connect, in this order
            self._publish_status("online")
            self._publish_announce()
            self.t.subscribe(self.base + "cmd", 1)
            self._publish_state()

    def on_disconnect(self):
        if self.stopped:
            return
        with self.lock:
            if not self.link_up:
                return
            self.link_up = False
            self.lost_since = self.clock.monotonic()
            self.log.info("link lost")

    def on_message(self, topic, payload: bytes):
        if self.stopped:
            return
        with self.lock:
            if self.stopped or topic != self.base + "cmd":
                return
            for _ in range(1 + self.faults.duplicate_cmd):    # fault: duplicate delivery
                self._handle_cmd_payload(payload)

    # ---------------------------------------------------------------- publishing (all schema-checked)
    def _publish_status(self, text):
        assert text in ("online", "offline")
        self.t.publish(self.base + "status", text, 1, True)

    def _send(self, kind, suffix, obj, retain):
        """Validate against the contract schema, then publish. A violation is a sim bug: counted, logged, not sent."""
        try:
            self.v.validate(kind, obj)
        except SchemaViolation as e:
            self.stats["violations"] += 1
            self.violation_log.append(str(e))
            self.log.error("refusing to publish: %s", e)
            return False
        return self.t.publish(self.base + suffix, json.dumps(obj, separators=(",", ":")), 1, retain)

    def build_state(self):
        eps = {}
        for ep in self.cfg.endpoints:
            vals = dict(self.values[ep.local_id])
            if ep.local_id in self.covers:
                vals["position"] = self.covers[ep.local_id].position
            eps[ep.local_id] = vals
        return {"ts": self.clock.wall(), "endpoints": eps}

    def _publish_state(self):
        ok = self._send("state", "state", self.build_state(), True)
        if ok:
            self.last_state_pub = self.clock.monotonic()

    def _publish_announce(self):
        mode = self.faults.malformed_announce
        ann = self.cfg.announce()
        if mode is None:
            self._send("announce", "announce", ann, True)
            return
        # deliberate malformed-announce fault: the only place that skips schema validation (and says so)
        self.log.warning("FAULT malformed_announce=%s: publishing a deliberately bad announce", mode)
        if mode == "invalid_json":
            raw = json.dumps(ann)[:60]
        else:
            if mode == "missing_field":
                del ann["hw"]
            elif mode == "wrong_schema":
                ann["schema"] = 1
            elif mode == "node_id_mismatch":
                other = self.id[:-1] + ("0" if self.id[-1] != "0" else "1")
                ann["node_id"] = other
            elif mode == "oversize":
                ann["padding"] = "x" * 17000
            elif mode == "unknown_capability":
                ann["endpoints"][0]["capabilities"].append(
                    {"id": "teleport", "kind": "actuator", "type": "bool", "boot_value": False})
            raw = json.dumps(ann, separators=(",", ":"))
        self.t.publish(self.base + "announce", raw, 1, True)

    # ---------------------------------------------------------------- commands
    def _handle_cmd_payload(self, payload: bytes):
        try:
            obj = json.loads(payload)
        except ValueError:
            self.log.warning("cmd is not JSON, dropped (no cmd_id to ack)")
            return
        cid = obj.get("cmd_id") if isinstance(obj, dict) else None
        if not isinstance(cid, str) or not UUID_RE.match(cid):
            self.log.warning("cmd without a valid cmd_id, dropped")
            return
        if cid in self.dedup:                             # retry/duplicate: replay the ack, never re-apply
            self.stats["cmds_deduped"] += 1
            self._send_ack(self.dedup[cid])
            return
        ack = self._execute(cid, obj)
        self.dedup[cid] = ack
        while len(self.dedup) > self.cfg.dedup_size:
            self.dedup.popitem(last=False)
        self._send_ack(ack)
        if ack["ok"] and obj.get("cmd") == "unprovision":
            self._schedule(self.faults.delay_ack_s + 0.2, self._unprovision)

    @staticmethod
    def _ack(cid, ok, endpoint=None, values=None, error=None):
        return {"cmd_id": cid, "ok": ok, "endpoint": endpoint, "values": values or {}, "error": error}

    def _execute(self, cid, obj):
        now = self.clock.monotonic()
        cmd = obj.get("cmd")
        if cmd == "identify":
            d = obj.get("duration_s")
            if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 60:
                return self._ack(cid, False, error="bad_request")
            self.identify_until = now + d
            self.log.info("IDENTIFY: blinking LED for %ss", d)
            return self._ack(cid, True)
        if cmd == "unprovision":
            self.log.info("UNPROVISION requested")
            return self._ack(cid, True)
        if cmd is not None:
            return self._ack(cid, False, error="unsupported")
        ep_id, sets = obj.get("endpoint"), obj.get("set")
        ep_out = ep_id if isinstance(ep_id, str) and EP_RE.match(ep_id) else None
        if (not isinstance(ep_id, str) or not isinstance(sets, dict) or not sets
                or not isinstance(obj.get("by"), str)):
            return self._ack(cid, False, ep_out, error="bad_request")
        if ep_id not in self.caps:
            return self._ack(cid, False, ep_out, error="unknown_endpoint")
        # validate everything first: a command is applied completely or not at all
        for cap_id, val in sets.items():
            c = self.caps[ep_id].get(cap_id)
            if c is None:
                return self._ack(cid, False, ep_out, error="unknown_capability")
            if c.kind != "actuator":
                return self._ack(cid, False, ep_out, error="unsupported")
            err = self._check(c, val)
            if err:
                return self._ack(cid, False, ep_out, error=err)
        for cap_id, val in sets.items():
            self._set_value(ep_id, cap_id, val, now)
        self.stats["cmds_applied"] += 1
        self._publish_state()
        return self._ack(cid, True, ep_id, dict(sets))

    @staticmethod
    def _check(c, val):
        """bad_type / out_of_range against the node's own announce."""
        if c.type == "bool":
            return None if isinstance(val, bool) else "bad_type"
        if c.type == "enum":
            if not isinstance(val, str):
                return "bad_type"
            return None if val in (c.values or []) else "out_of_range"
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return "bad_type"
        if c.type == "int" and not isinstance(val, int):
            return "bad_type"
        lo, hi = c.effective_range()
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            return "out_of_range"
        return None

    def _set_value(self, ep_id, cap_id, val, now):
        c = self.caps[ep_id][cap_id]
        if cap_id == "position" and ep_id in self.covers:
            self.covers[ep_id].set_target(val, now)
            return
        self.values[ep_id][cap_id] = val
        if c.max_on_s:
            key = (ep_id, cap_id)
            if val is True:
                self.on_since.setdefault(key, now)       # continuous on-time: a repeat "on" does not restart it
            else:
                self.on_since.pop(key, None)

    # ---------------------------------------------------------------- acks (with fault injection)
    def _send_ack(self, ack):
        f = self.faults
        if self._drop_first > 0:
            self._drop_first -= 1
            drop = True
        else:
            drop = f.drop_ack > 0 and self.rng.random() < f.drop_ack
        if drop:
            self.stats["acks_dropped"] += 1
            self.log.warning("FAULT drop_ack: ack for %s not sent", ack["cmd_id"])
            return
        if f.delay_ack_s > 0:
            self._schedule(f.delay_ack_s, lambda a=ack: self._emit_ack(a))
        else:
            self._emit_ack(ack)

    def _emit_ack(self, ack):
        for _ in range(2 if self.faults.duplicate_ack else 1):
            if self._send("ack", "ack", ack, False):
                self.stats["acks_sent"] += 1

    def _schedule(self, delay, fn):
        self._seq += 1
        heapq.heappush(self._sched, (self.clock.monotonic() + delay, self._seq, fn))

    def _unprovision(self):
        """Wipe config and return to provisioning: say offline, disconnect, stay down (no BLE in the sim)."""
        self.log.info("config wiped, back to provisioning mode (sim stays disconnected)")
        self._publish_status("offline")
        self.provisioned = False
        self.stopped = True
        self.link_up = False
        self.t.flush()
        self.t.close()

    # ---------------------------------------------------------------- run loop
    def tick(self):
        with self.lock:
            if self.stopped:
                return
            now = self.clock.monotonic()
            while self._sched and self._sched[0][0] <= now:
                _, _, fn = heapq.heappop(self._sched)
                fn()
                if self.stopped:
                    return
            publish = False
            for ep_id, cov in self.covers.items():
                was_busy = cov.busy
                cov.update(now)
                if cov.busy and now >= self.next_progress[ep_id]:
                    self.next_progress[ep_id] = now + self._ep(ep_id).progress_s
                    publish = True
                if was_busy and not cov.busy:
                    publish = True
            for (ep_id, cap_id), since in list(self.on_since.items()):
                limit = self.caps[ep_id][cap_id].max_on_s
                if now - since >= limit:
                    self.log.warning("max_on_s=%s reached on %s.%s, forcing off", limit, ep_id, cap_id)
                    self._set_value(ep_id, cap_id, False, now)
                    self.stats["max_on_trips"] += 1
                    publish = True
            self._check_failsafe(now)
            due = [k for k, t in self.next_sample.items() if now >= t]
            for k in due:
                self.values[k[0]][k[1]] = self.gens[k].sample(now)
                self.next_sample[k] = now + self.caps[k[0]][k[1]].interval_s
                publish = True
            if self.link_up and (publish or now - self.last_state_pub >= self.cfg.heartbeat_s):
                self._publish_state()
            if self.identify_until is not None and now >= self.identify_until:
                self.identify_until = None
                self.log.info("identify finished")
            fl = self.faults.flapping
            if fl and self.link_up and self.connected_at is not None and now - self.connected_at >= fl[0]:
                self.log.warning("FAULT flapping: dropping the connection for %ss", fl[1])
                self.connected_at = None
                self.t.drop(fl[1])

    def shutdown(self):
        """Orderly stop (Ctrl-C): a clean MQTT disconnect does not fire the LWT, so say offline ourselves."""
        with self.lock:
            if self.link_up and not self.stopped:
                self._publish_status("offline")
                self.t.flush()
            self.stopped = True
            self.t.close()

    def _ep(self, ep_id):
        return next(e for e in self.cfg.endpoints if e.local_id == ep_id)

    def _check_failsafe(self, now):
        after = self.cfg.failsafe_after_s
        if self.link_up or self.failsafe_done or after == 0 or self.lost_since is None:
            return
        if now - self.lost_since < after:
            return
        self.failsafe_done = True
        self.stats["failsafes"] += 1
        self.log.warning("hub lost for %ss: applying failsafe_value", after)
        for ep in self.cfg.endpoints:
            for c in ep.caps:
                if c.kind == "actuator" and c.has_failsafe:
                    self._set_value(ep.local_id, c.id, c.failsafe_value, now)
