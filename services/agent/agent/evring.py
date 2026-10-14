"""Agent-originated events for the gateway (`GET /v1/events`): a bounded in-memory ring with monotonic ids, tagged with the
owning `user_id` or "household", replayable with Last-Event-ID.

What goes in: `notification`s always; and the other chat events of the contract (`async_result`, `action_settled`,
`action_settle_timeout`, `confirmation_request`) whenever their chat stream is not available to carry them (the client
disconnected, the stream already closed, or the action was started by an approval hand-off that has no stream). While the
chat stream is open the gateway already sees them there, so they are not duplicated here.

Replay: at least the last 200 events or 10 minutes (we keep up to MAX_EVENTS and never drop an event younger than
MIN_AGE_S). A Last-Event-ID older than the buffer, or from a previous agent run, gets `event: reset` first."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

STREAM_EVENTS = ("async_result", "action_settled", "action_settle_timeout", "confirmation_request")
MAX_EVENTS = 2000
MIN_AGE_S = 600.0


@dataclass
class RingEvent:
    id: int
    name: str
    data: dict
    ts: float = field(default_factory=time.monotonic)


class EventRing:
    def __init__(self, max_events: int = MAX_EVENTS, min_age_s: float = MIN_AGE_S) -> None:
        self._buf: deque[RingEvent] = deque()
        self._max, self._min_age = max_events, min_age_s
        self._seq = 0
        self._subs: list[asyncio.Queue] = []

    @property
    def last_id(self) -> int:
        return self._seq

    def publish(self, name: str, data: dict, user_id: str | None) -> RingEvent:
        self._seq += 1
        ev = RingEvent(self._seq, name, {**data, "user_id": user_id or "household"})
        self._buf.append(ev)
        now = time.monotonic()
        while len(self._buf) > self._max or (len(self._buf) > 200 and now - self._buf[0].ts > self._min_age):
            self._buf.popleft()
        for q in list(self._subs):
            q.put_nowait(ev)
        return ev

    def replay_from(self, last_event_id: str | None) -> tuple[bool, list[RingEvent]]:
        """(reset, events with id > last_event_id). No header = live only (nothing replayed, no reset)."""
        if last_event_id is None:
            return False, []
        try:
            n = int(last_event_id)
        except ValueError:
            return True, list(self._buf)
        oldest = self._buf[0].id if self._buf else self._seq + 1
        if n > self._seq or n < oldest - 1:                  # from a previous run, or older than what we still hold
            return True, list(self._buf)
        return False, [e for e in self._buf if e.id > n]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)


class RingNotifier:
    """Notifier that pushes `notification` events (contracts/agent-events.md) into the ring; the gateway forwards them on
    its WebSocket. `user_id=None` means household."""

    def __init__(self, ring: EventRing, keep: int = 100) -> None:
        self.ring = ring
        self.items: deque[dict] = deque(maxlen=keep)

    async def notify(self, user_id: str | None, level: str, text: str) -> None:
        payload = {"type": "notification", "level": level, "text": text}
        self.items.append({"user_id": user_id, **payload})
        self.ring.publish("notification", payload, user_id)
