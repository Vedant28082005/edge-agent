"""Registry-change event stream: in-process hooks plus a replayable ring buffer for the subscribable endpoint.

Event types:
  registry_changed  endpoints/capabilities/names/rooms/risk/enabled changed. The agent rebuilds its prompt device list
                    and its constrained-decoding schema. Data: {version, reason, endpoint_ids}
  scenes_changed    a scene was created/updated/deleted or flagged/unflagged broken. Data: {reason, scene_ids}
  node_status       a node went online/offline.            Data: {node_id, online}
  state             live values changed (opt-in).          Data: {endpoint_id, values, ts}
  action_settled / action_settle_timeout  a motion (settle_s) reached / missed its target.
                    Data: {endpoint_id, capability, value, observed, elapsed_ms | settle_s, call_id?}
  pending_node      a node announced and awaits approval.  Data: {node_id}
"""
from __future__ import annotations

import collections
import logging
import queue
import threading
from typing import Any, Callable

log = logging.getLogger("device_hub.events")

DEFAULT_TYPES = frozenset({"registry_changed", "scenes_changed", "node_status", "pending_node", "action_settled",
                           "action_settle_timeout"})
ALL_TYPES = DEFAULT_TYPES | {"state"}


class Subscription:
    def __init__(self, bus: "EventBus", types: frozenset[str]) -> None:
        self.bus = bus
        self.types = types
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self.dropped = False

    def get(self, timeout: float) -> dict | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.bus._remove(self)


class EventBus:
    def __init__(self, history: int = 1000) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: collections.deque[dict] = collections.deque(maxlen=history)
        self._hooks: list[Callable[[dict], None]] = []
        self._subs: list[Subscription] = []

    def add_hook(self, fn: Callable[[dict], None]) -> None:
        """In-process hook, called synchronously on the publisher's thread. Must not block."""
        self._hooks.append(fn)

    def publish(self, etype: str, data: dict[str, Any]) -> dict:
        import time

        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "type": etype, "ts": int(time.time()), "data": data}
            self._ring.append(ev)
            subs = list(self._subs)
        for fn in list(self._hooks):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                log.exception("event hook failed")
        for s in subs:
            if etype in s.types:
                try:
                    s.q.put_nowait(ev)
                except queue.Full:
                    s.dropped = True  # slow consumer: it must resync from the registry (seq gap shows it)
        return ev

    def subscribe(self, types: frozenset[str] | None = None, since_seq: int | None = None) -> Subscription:
        sub = Subscription(self, frozenset(types) if types else DEFAULT_TYPES)
        with self._lock:
            if since_seq is not None:
                for ev in self._ring:
                    if ev["seq"] > since_seq and ev["type"] in sub.types:
                        sub.q.put_nowait(ev)
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq
