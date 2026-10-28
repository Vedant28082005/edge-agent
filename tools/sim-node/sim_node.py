#!/usr/bin/env python3
"""Simulated ESP32 node for the Pi stack.

A thin layer over the sim-esp32 engine (tools/sim-esp32/simesp32), which already implements the node side of
contracts/mqtt.md (connect lifecycle, retained announce, LWT, state, cmd dedup ring, ack, failsafe, max_on_s) and
validates every outgoing announce/state/ack against contracts/*.schema.json. This file adds:
  * profiles mirroring firmware/examples (profiles.py)
  * --node-id aliases (the contract id is esp32-<12 hex>; a label such as sim-fan-01 maps to one deterministically)
  * simulated command latency (default 50..300 ms before the command is applied; state then ack follow, as in firmware)
  * --fail-rate: a fraction of set-commands is refused with ack error `busy` and not applied
  * one log line per command
  * credentials from .sim/<node_id>/ (written by provision-sim.ps1), never from argv

Run:  python sim_node.py --profile fan --node-id sim-fan-01
"""
import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sim-esp32"))

from simesp32.clock import Clock  # noqa: E402
from simesp32.config import NODE_ID_RE, ConfigError, _node  # noqa: E402
from simesp32.creds import CredError, credentials_for  # noqa: E402
from simesp32.node import EP_RE, PREFIX, Node  # noqa: E402
from simesp32.schemas import Validator  # noqa: E402
from simesp32.transport import MemoryBus, MemoryTransport, PahoTransport  # noqa: E402

from profiles import PROFILES  # noqa: E402

DEFAULT_HOST = "192.168.29.89"
DEFAULT_PORT = 8883
SIM_FW = "sim-0.1.0"
EXIT_CREDENTIALS = 3
ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]{1,48}$")


def resolve_node_id(value: str) -> str:
    """A real contract id is used as is; any other label maps to esp32-<first 12 hex of sha256(label)>.
    provision-sim.ps1 uses the same rule, so both sides agree on the id without sharing a file."""
    if NODE_ID_RE.match(value):
        return value
    if not ALIAS_RE.match(value):
        raise ConfigError(f"--node-id {value!r}: use esp32-<12 hex> or a label of [A-Za-z0-9._-], max 48 chars")
    return "esp32-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def build_config(profile: str, node_id: str, heartbeat_s: float = 60.0, seed: int | None = None):
    """Profile dict -> NodeConfig (hub cross-field rules enforced by the sim-esp32 loader)."""
    p = PROFILES[profile]
    raw = {"node_id": node_id, "hw": p["hw"], "fw": SIM_FW, "heartbeat_s": heartbeat_s,
           "seed": seed if seed is not None else int(hashlib.sha256(node_id.encode()).hexdigest()[:8], 16),
           "endpoints": p["endpoints"]}
    return _node(json.loads(json.dumps(raw)), 0)


def parse_latency(text: str) -> tuple:
    try:
        lo, _, hi = text.partition(":")
        lo_f, hi_f = float(lo), float(hi or lo)
    except ValueError:
        raise ConfigError(f"--latency-ms {text!r}: use MIN:MAX or a single number") from None
    if lo_f < 0 or hi_f < lo_f:
        raise ConfigError("--latency-ms: need 0 <= MIN <= MAX")
    return lo_f, hi_f


AUTH_REFUSALS_TO_EXIT = 3


def is_auth_refusal(reason_code) -> bool:
    text = str(reason_code).lower()
    return "bad user name or password" in text or "not authorized" in text


class AuthWatchTransport(PahoTransport):
    """PahoTransport already retries the first connection and every reconnect with 1..30 s backoff (a broker that is
    down, no LAN yet, a TLS error: the sim keeps trying). Only a broker that keeps refusing the credentials is final:
    that is counted here so the main loop can exit with a clear message instead of retrying forever."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.auth_refusals = 0

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.auth_refusals = self.auth_refusals + 1 if is_auth_refusal(reason_code) else 0
        else:
            self.auth_refusals = 0
        super()._on_connect(client, userdata, flags, reason_code, properties)


class SimNode(Node):
    """Node + simulated processing latency and optional command failures."""

    def __init__(self, cfg, transport, validator, clock=None, *, latency_ms=(50.0, 300.0), fail_rate=0.0,
                 rng_seed=None):
        super().__init__(cfg, transport, validator, clock)
        if not 0.0 <= fail_rate <= 1.0:
            raise ValueError("fail_rate must be 0..1")
        self.latency_ms = latency_ms
        self.fail_rate = fail_rate
        self.sim_rng = random.Random(rng_seed)
        self._last_due = float("-inf")
        self._cur_latency_ms = 0.0
        self.stats["cmds_failed_injected"] = 0

    # Commands are handled `latency` after arrival, in arrival order (a real node works through its queue in order).
    def on_message(self, topic, payload: bytes):
        if self.stopped:
            return
        with self.lock:
            if self.stopped or topic != self.base + "cmd":
                return
            for _ in range(1 + self.faults.duplicate_cmd):
                lat_ms = self.sim_rng.uniform(*self.latency_ms)
                if lat_ms <= 0:
                    self._handle_with_latency(payload, 0.0)
                    continue
                now = self.clock.monotonic()
                due = max(now + lat_ms / 1000.0, self._last_due + 0.001)
                self._last_due = due
                self._schedule(due - now, lambda p=payload, ms=lat_ms: self._handle_with_latency(p, ms))

    def _handle_with_latency(self, payload, lat_ms):
        self._cur_latency_ms = lat_ms
        before = self.stats["cmds_deduped"]
        self._handle_cmd_payload(payload)
        if self.stats["cmds_deduped"] > before:
            self.log.info("cmd repeated cmd_id -> cached ack replayed, not re-applied")

    def _execute(self, cid, obj):
        is_set = obj.get("cmd") is None
        if is_set and self.fail_rate > 0 and self.sim_rng.random() < self.fail_rate:
            ep = obj.get("endpoint")
            ack = self._ack(cid, False, ep if isinstance(ep, str) and EP_RE.match(ep) else None, error="busy")
            self.stats["cmds_failed_injected"] += 1
        else:
            ack = super()._execute(cid, obj)
        self._log_cmd(obj, ack)
        return ack

    def _log_cmd(self, obj, ack):
        if obj.get("cmd"):
            what = f"{obj['cmd']}" + (f" {obj['duration_s']}s" if "duration_s" in obj else "")
        else:
            what = f"{obj.get('endpoint')} set={json.dumps(obj.get('set'), separators=(',', ':'))} by={obj.get('by')}"
        result = "ok" if ack["ok"] else f"ERR {ack['error']}"
        self.log.info("cmd %s %s -> %s (+%d ms)", str(obj.get("cmd_id"))[:8], what, result, round(self._cur_latency_ms))


def build_parser():
    p = argparse.ArgumentParser(prog="sim_node.py", description="Simulated ESP32 node (see README.md).")
    p.add_argument("--profile", required=True, choices=sorted(PROFILES))
    p.add_argument("--node-id", required=True,
                   help="esp32-<12 hex> as returned by provisioning, or a label such as sim-fan-01 (maps to a "
                        "deterministic esp32-<hex> id, printed at start)")
    p.add_argument("--host", default=os.environ.get("SIM_MQTT_HOST", DEFAULT_HOST))
    p.add_argument("--port", type=int, default=int(os.environ.get("SIM_MQTT_PORT", DEFAULT_PORT)))
    p.add_argument("--sim-dir", default=str(HERE / ".sim"), help="folder holding <node_id>/ written by provision-sim.ps1")
    p.add_argument("--ca", default=None, help="CA PEM (default: <sim-dir>/<node_id>/ca.pem)")
    p.add_argument("--latency-ms", default="50:300", metavar="MIN:MAX", help="command processing latency (default 50:300)")
    p.add_argument("--fail-rate", type=float, default=0.0, help="0..1 share of set-commands refused with ack error `busy`")
    p.add_argument("--heartbeat-s", type=float, default=60.0, help="state republished at least this often")
    p.add_argument("--seed", type=int, default=None, help="seed for sensors, latency and failures (default: from node id)")
    p.add_argument("--contracts", default=None, help="contracts/ folder (default: <repo>/contracts)")
    p.add_argument("--mock", action="store_true", help="no broker: in-memory bus, messages printed; stdin lines are cmd JSON")
    p.add_argument("--check", action="store_true", help="print the announce and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        node_id = resolve_node_id(args.node_id)
        latency = parse_latency(args.latency_ms)
        if not 0.0 <= args.fail_rate <= 1.0:
            raise ConfigError("--fail-rate must be 0..1")
        cfg = build_config(args.profile, node_id, args.heartbeat_s, args.seed)
        validator = Validator(Path(args.contracts) if args.contracts else None)
        validator.validate("announce", cfg.announce())
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"node_id {node_id}  profile {args.profile}  hw {cfg.hw}  fw {cfg.fw}"
          + (f"  (from label {args.node_id!r})" if node_id != args.node_id else ""))
    if args.check:
        print(json.dumps(cfg.announce(), indent=2))
        return 0

    clock = Clock()
    will = f"{PREFIX}{node_id}/status"
    bus = None
    if args.mock:
        bus = MemoryBus(echo=print)
        transport = MemoryTransport(node_id, bus, clock, will)
    else:
        node_dir = Path(args.sim_dir) / node_id
        ca = Path(args.ca) if args.ca else node_dir / "ca.pem"
        try:
            if not ca.is_file():
                raise CredError(f"CA file not found: {ca} (run provision-sim.ps1 first)")
            user, pw = credentials_for(node_id, node_dir, True)
        except CredError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        transport = AuthWatchTransport(node_id, args.host, args.port, ca, user, pw, will)
    node = SimNode(cfg, transport, validator, clock, latency_ms=latency, fail_rate=args.fail_rate,
                   rng_seed=cfg.seed)
    node.boot()
    transport.start()
    print(f"running ({'mock bus' if args.mock else f'{args.host}:{args.port} TLS'}), Ctrl-C to stop; "
          "retries the broker with backoff until it is reachable", flush=True)
    if args.mock:
        import threading
        threading.Thread(target=_stdin_injector, args=(bus, node_id), daemon=True).start()
    rc = 0
    try:
        while not node.stopped:
            if args.mock:
                transport.poll()
            node.tick()
            if getattr(transport, "auth_refusals", 0) >= AUTH_REFUSALS_TO_EXIT:
                logging.error("credentials rejected by the broker (expired or removed). Re-run: "
                              "provision-sim.ps1 -NodeId %s -GatewayUrl <gateway>", args.node_id)
                rc = EXIT_CREDENTIALS
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()            # publishes status=offline (a clean disconnect would not fire the LWT)
    print(node_id, json.dumps(node.stats))
    return rc


def _stdin_injector(bus, node_id):
    for line in sys.stdin:
        line = line.strip()
        if line:
            bus.inject(node_id, line)


if __name__ == "__main__":
    sys.exit(main())
