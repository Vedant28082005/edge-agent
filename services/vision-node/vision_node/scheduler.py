"""One heavy job at a time, bounded waiting queue, STT first. Deadlines include queue wait."""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field

from .errors import ApiError

PRIO_STT = 0
PRIO_OTHER = 1


@dataclass(order=True)
class _Waiter:
    prio: int
    seq: int
    fut: asyncio.Future = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class Scheduler:
    def __init__(self, max_waiting: int = 4, max_inflight: int = 7):
        self.max_waiting = max_waiting
        self.max_inflight = max_inflight     # admitted requests (reading body, waiting, running): bounds RAM held by uploads
        self._heap: list[_Waiter] = []
        self._live = 0                       # waiters not cancelled
        self._busy = False
        self._inflight = 0
        self._seq = itertools.count()

    @property
    def depth(self) -> int:
        return self._live

    @property
    def busy(self) -> bool:
        return self._busy

    # -- admission: checked BEFORE the request body is read, so a flood of uploads cannot pile up in RAM --------------
    def admit(self) -> None:
        if self._inflight >= self.max_inflight or (self._busy and self._live >= self.max_waiting):
            raise ApiError("queue_full", retry_after=2)
        self._inflight += 1

    def leave(self) -> None:
        self._inflight -= 1

    # -- the slot ------------------------------------------------------------------------------------------------------
    async def acquire(self, prio: int, deadline: float) -> float:
        """Returns the seconds spent waiting. Raises queue_full / timeout."""
        t0 = time.monotonic()
        if not self._busy and self._live == 0:
            self._busy = True
            return 0.0
        if self._live >= self.max_waiting:
            raise ApiError("queue_full", retry_after=2)
        w = _Waiter(prio, next(self._seq), asyncio.get_running_loop().create_future())
        heapq.heappush(self._heap, w)
        self._live += 1
        try:
            await asyncio.wait_for(asyncio.shield(w.fut), max(0.0, deadline - t0))
        except asyncio.TimeoutError:
            self._abandon(w)
            raise ApiError("timeout")
        except BaseException:
            self._abandon(w)
            raise
        return time.monotonic() - t0

    def _abandon(self, w: _Waiter) -> None:
        if w.fut.done() and not w.fut.cancelled():
            # granted at the same moment we gave up: hand the slot on instead of leaking it
            self.release()
        elif not w.cancelled:
            w.cancelled = True
            self._live -= 1

    def release(self) -> None:
        self._busy = False
        while self._heap:
            w = heapq.heappop(self._heap)
            if w.cancelled:
                continue
            self._live -= 1
            self._busy = True
            w.fut.set_result(None)
            return
