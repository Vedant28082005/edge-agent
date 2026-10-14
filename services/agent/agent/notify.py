"""Notifications for things that have no open request stream: blocked proactive actions, late settle timeouts.
The contract says these go over the gateway WebSocket (`{"type":"notification","level":...,"text":...}`) but defines no
agent->gateway channel, so v1 only logs and keeps the last entries in memory (see README, decisions)."""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Protocol

log = logging.getLogger("agent.notify")


class Notifier(Protocol):
    async def notify(self, user_id: str | None, level: str, text: str) -> None: ...


class LogNotifier:
    def __init__(self, keep: int = 100) -> None:
        self.items: deque[dict] = deque(maxlen=keep)

    async def notify(self, user_id: str | None, level: str, text: str) -> None:
        self.items.append({"ts": time.time(), "user_id": user_id, "type": "notification", "level": level, "text": text})
        log.info("notification level=%s text=%s", level, text)
