"""Clock abstraction so safety timers and travel time are testable without sleeping."""
import time


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> int:
        return int(time.time())


class ManualClock(Clock):
    def __init__(self, start_wall: int = 1_760_000_000):
        self._t = 0.0
        self._wall0 = start_wall

    def monotonic(self) -> float:
        return self._t

    def wall(self) -> int:
        return self._wall0 + int(self._t)

    def advance(self, dt: float) -> None:
        self._t += dt
