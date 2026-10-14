"""Per-request context: the SSE output queue (monotonic `seq`, `req_id` on every event), the budgets, and the
bookkeeping for settle events that arrive after `final`."""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .policy import Trigger


@dataclass
class Budget:
    """Per-request budgets: LLM rounds (`max_steps`), tool calls, wall-clock deadline, retries of idempotent calls.
    Time spent waiting for the user's confirmation is excluded from the deadline (`pause`)."""
    max_steps: int = 4
    max_calls: int = 8
    deadline_s: float = 180.0
    max_retries: int = 1
    steps: int = 0
    calls: int = 0
    retries: int = 0
    started: float = field(default_factory=time.monotonic)
    paused_s: float = 0.0

    def elapsed(self) -> float:
        return time.monotonic() - self.started - self.paused_s

    def time_left(self) -> float:
        return self.deadline_s - self.elapsed()

    def take_step(self) -> bool:
        if self.steps >= self.max_steps or self.time_left() <= 0:
            return False
        self.steps += 1
        return True

    def take_call(self) -> bool:
        if self.calls >= self.max_calls or self.time_left() <= 0:
            return False
        self.calls += 1
        return True

    def take_retry(self) -> bool:
        if self.retries >= self.max_retries or self.time_left() <= 0:
            return False
        self.retries += 1
        return True


class ReqCtx:
    def __init__(self, user_id: str, session_id: str, text: str, trigger: Trigger, budget: Budget,
                 req_id: str | None = None, role: str = "member", images: int = 0, sink=None) -> None:
        self.req_id = req_id or "r_" + secrets.token_hex(6)
        self.user_id, self.session_id, self.text = user_id, session_id, text
        self.role = role                    # set only by the authenticated gateway; never inferred
        self.images = images                # count of validated inline images (not analysed in v1)
        self.sink = sink                    # (name, data, user_id) -> None: where events go when the stream is gone
        self.trigger, self.budget = trigger, budget
        self.out: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        self._seq = 0
        self.detached = False               # the HTTP client went away: late events go to the notifier instead
        self.closed = False
        self.pending_settles = 0
        self._settled = asyncio.Event()
        self._settled.set()
        self.reason = f"user request: {text[:100]}"
        self.label = ""                     # human name of a non-user trigger (a rule's name)

    async def emit(self, name: str, data: dict[str, Any]) -> bool:
        """Queue an SSE event. When the stream is gone (detached/closed/never existed) the contract's late-event kinds go to
        the gateway event ring instead (`sink`); returns True if the event was delivered somewhere."""
        if self.detached or self.closed:
            if self.sink is not None and name in ("async_result", "action_settled", "action_settle_timeout",
                                                  "confirmation_request"):
                self.sink(name, {**data, "req_id": self.req_id}, self.user_id if self.trigger.is_user else None)
                return True
            return False
        self._seq += 1
        await self.out.put((name, {**data, "seq": self._seq, "req_id": self.req_id}))
        return True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.out.put_nowait(None)

    # settle bookkeeping (see SettleTable)
    def settle_added(self) -> None:
        self.pending_settles += 1
        self._settled.clear()

    def settle_done(self) -> None:
        self.pending_settles = max(0, self.pending_settles - 1)
        if self.pending_settles == 0:
            self._settled.set()

    async def wait_settles(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._settled.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
