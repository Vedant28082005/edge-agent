"""MQTT bridge: node topics in (announce/status/state/ack), commands out, ack correlation.

Everything a node sends is untrusted. Announces are validated against the contract schema plus the hub rules and
rejected with a logged reason; status is a bare string; state/ack are schema-validated. Nothing here ever queues a
command: if the broker connection is down the publish fails.
"""
from __future__ import annotations

import collections
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .contracts import MAX_PAYLOAD_BYTES, AnnounceRejected, Contracts
from .errors import HubError
from .events import EventBus
from .registry import Registry
from .transport import Transport, TransportDown
from .util import safe_repr

log = logging.getLogger("device_hub.bridge")

PREFIX = "home/v1/"
TOPIC_RE = re.compile(r"^home/v1/nodes/(esp32-[0-9a-f]{12})/(announce|status|state|ack)$")
SUBSCRIPTIONS = [PREFIX + f"nodes/+/{leaf}" for leaf in ("announce", "status", "state", "ack")]


@dataclass
class PendingCmd:
    cmd_id: str
    node_id: str
    event: threading.Event = field(default_factory=threading.Event)
    ack: dict | None = None
    duplicates: int = 0


class MqttBridge:
    def __init__(self, transport: Transport, registry: Registry, contracts: Contracts, bus: EventBus,
                 on_announce_accepted: Callable[[str], None] | None = None) -> None:
        self.t = transport
        self.registry = registry
        self.contracts = contracts
        self.bus = bus
        self.on_announce_accepted = on_announce_accepted
        self._pending: dict[str, PendingCmd] = {}
        self._lock = threading.Lock()
        self.rejections: collections.deque[dict] = collections.deque(maxlen=100)
        self.stats: collections.Counter[str] = collections.Counter()
        transport.add_message_handler(self.on_message)
        for s in SUBSCRIPTIONS:
            transport.subscribe(s, 1)

    # ------------------------------------------------------------------------------------------ inbound
    def on_message(self, topic: str, payload: bytes, retain: bool) -> None:
        m = TOPIC_RE.match(topic)
        if not m:
            return
        node_id, leaf = m.group(1), m.group(2)
        if len(payload) > MAX_PAYLOAD_BYTES and leaf != "announce":
            self.stats[f"{leaf}_oversize"] += 1
            log.warning("%s payload too large from %s (%d bytes), dropped", leaf, node_id, len(payload))
            return
        if payload == b"":
            return  # a retained-topic clear (our own forget cleanup, delivered with retain=0), nothing to process
        if self.registry.node_exists(node_id, include_deleted=True) and not self.registry.node_exists(node_id):
            self.stats["forgotten_node_messages"] += 1
            return  # forgotten node: ignore everything it sends
        getattr(self, f"_on_{leaf}")(node_id, payload)

    def _reject(self, node_id: str, reason: str) -> None:
        self.stats["announce_rejected"] += 1
        self.rejections.append({"node_id": node_id, "reason": reason, "ts": self.registry.clock.now()})
        log.warning("announce rejected node=%s reason=%s", node_id, reason)

    def _on_announce(self, node_id: str, payload: bytes) -> None:
        try:
            ann = self.contracts.parse_announce(payload, node_id)
        except AnnounceRejected as e:
            self._reject(node_id, str(e))
            return
        try:
            res = self.registry.ingest_announce(node_id, ann)
        except HubError as e:
            self._reject(node_id, e.code)
            return
        self.stats["announce_accepted"] += 1
        if res.changed:
            log.info("announce accepted node=%s endpoints=%d pending=%d", node_id, len(ann["endpoints"]),
                     len(res.pending_local_ids))
        else:
            self.stats["announce_duplicate"] += 1
        if self.on_announce_accepted:
            self.on_announce_accepted(node_id)

    def _on_status(self, node_id: str, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace").strip()
        if text not in ("online", "offline"):
            self.stats["status_invalid"] += 1
            log.warning("invalid status from %s: %s", node_id, safe_repr(text, 30))
            return
        online = text == "online"
        if self.registry.live.set_online(node_id, online):
            self.bus.publish("node_status", {"node_id": node_id, "online": online})
            log.info("node %s is %s", node_id, text)

    def _on_state(self, node_id: str, payload: bytes) -> None:
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.stats["state_invalid"] += 1
            return
        err = self.contracts.validate_state(doc)
        if err:
            self.stats["state_invalid"] += 1
            log.warning("state rejected node=%s reason=%s", node_id, err)
            return
        changed = self.registry.ingest_state(node_id, doc)
        for local_id in changed:
            row = self.registry.db.one("SELECT endpoint_id FROM endpoints WHERE node_id=? AND local_id=? AND deleted=0",
                                       (node_id, local_id))
            if row:
                self.bus.publish("state", {"endpoint_id": row["endpoint_id"], "values": doc["endpoints"][local_id],
                                           "ts": doc["ts"]})

    def _on_ack(self, node_id: str, payload: bytes) -> None:
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.stats["ack_invalid"] += 1
            return
        err = self.contracts.validate_ack(doc)
        if err:
            self.stats["ack_invalid"] += 1
            log.warning("ack rejected node=%s reason=%s", node_id, err)
            return
        with self._lock:
            p = self._pending.get(doc["cmd_id"])
            if p is None or p.node_id != node_id:
                self.stats["ack_unmatched"] += 1      # late (after timeout), unknown, or from the wrong node
                return
            if p.ack is not None:
                p.duplicates += 1                     # QoS1 redelivery: first ack wins
                self.stats["ack_duplicate"] += 1
                return
            p.ack = doc
        p.event.set()

    # ------------------------------------------------------------------------------------------ outbound
    def send_cmd(self, node_id: str, cmd: dict, expect_ack: bool = True) -> PendingCmd | None:
        """Validate against cmd.schema.json and publish (QoS 1, not retained). Registers the ack waiter BEFORE
        publishing so a fast ack cannot be missed. Raises TransportDown when the broker link is down (never queued)."""
        err = self.contracts.validate_cmd(cmd)
        if err:
            raise HubError("internal_bad_cmd", f"hub built an invalid cmd: {err}", 500)
        pending = None
        if expect_ack:
            pending = PendingCmd(cmd["cmd_id"], node_id)
            with self._lock:
                self._pending[cmd["cmd_id"]] = pending
        try:
            self.t.publish(f"{PREFIX}nodes/{node_id}/cmd", json.dumps(cmd, separators=(",", ":")), qos=1, retain=False)
        except TransportDown:
            self.forget_pending(cmd["cmd_id"])
            raise
        return pending

    def forget_pending(self, cmd_id: str) -> None:
        with self._lock:
            self._pending.pop(cmd_id, None)

    def wait_ack(self, pending: PendingCmd, timeout: float) -> dict | None:
        try:
            if pending.event.wait(timeout):
                return pending.ack
            return None
        finally:
            self.forget_pending(pending.cmd_id)

    @staticmethod
    def new_cmd_id() -> str:
        return str(uuid.uuid4())

    def clear_retained(self, node_id: str) -> None:
        """After forget: remove the node's retained announce/status/state so nothing stale remains on the broker."""
        for leaf in ("announce", "status", "state"):
            try:
                self.t.publish(f"{PREFIX}nodes/{node_id}/{leaf}", b"", qos=1, retain=True)
            except TransportDown:
                log.warning("could not clear retained %s for %s (broker down)", leaf, node_id)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
