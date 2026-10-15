"""One inference at a time, bounded FIFO queue (contract: 8 waiting, 429 `queue_full` beyond), hard per-job deadline.

A job whose deadline passes while queued is dropped without running (504). A job whose deadline passes while running
returns 504 at once, but the slot stays held until the worker thread really finishes, so two inferences never overlap."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Deque, Tuple

from .errors import NodeError


class Scheduler:
    def __init__(self, max_queue: int = 8) -> None:
        self.max_queue = max_queue
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="infer")
        self._held = False
        self._q: Deque[asyncio.Future] = deque()
        self._waiting = 0

    @property
    def depth(self) -> int:
        return self._waiting

    @property
    def busy(self) -> bool:
        return self._held

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    async def _acquire(self, remaining: float) -> None:
        if not self._held and not self._q:
            self._held = True
            return
        fut = asyncio.get_running_loop().create_future()
        self._q.append(fut)
        try:
            await asyncio.wait_for(fut, remaining)
        except asyncio.TimeoutError:
            raise NodeError("timeout")
        except BaseException:
            if fut.done() and not fut.cancelled():
                self._release()          # ownership was handed to us just as we were cancelled: pass it on
            raise

    def _release(self) -> None:
        while self._q:
            f = self._q.popleft()
            if not f.done():
                f.set_result(None)       # the slot stays held, now owned by f's waiter
                return
        self._held = False

    async def run(self, fn: Callable[[float], Any], deadline: float) -> Tuple[Any, float]:
        """Run fn(queue_ms) in the single inference thread. `deadline` is a time.monotonic() value.
        Returns (result, queue_ms). Raises NodeError(queue_full | timeout) or whatever fn raises."""
        if self._waiting >= self.max_queue:
            raise NodeError("queue_full", retry_after=2)
        enq = time.monotonic()
        self._waiting += 1
        try:
            remaining = deadline - enq
            if remaining <= 0:
                raise NodeError("timeout")
            await self._acquire(remaining)
        finally:
            self._waiting -= 1
        queue_ms = (time.monotonic() - enq) * 1000.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._release()
            raise NodeError("timeout")
        fut = asyncio.get_running_loop().run_in_executor(self._pool, fn, queue_ms)

        def _done(f: "asyncio.Future") -> None:
            if not f.cancelled():
                f.exception()            # mark retrieved
            self._release()

        fut.add_done_callback(_done)     # the slot is released only when the thread finished, whatever happens here
        try:
            res = await asyncio.wait_for(asyncio.shield(fut), remaining)
        except asyncio.TimeoutError:
            raise NodeError("timeout")
        return res, queue_ms
