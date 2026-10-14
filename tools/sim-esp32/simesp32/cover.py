"""Curtain/blind mechanics: travel time and an up/down relay interlock.

Two relays (`up` raises position, `down` lowers it) are never energised together, and a relay is only
energised after the opposite one has been off for at least `interlock_s` (protects the motor on reversal).
"""


class Cover:
    EPS = 1e-6

    def __init__(self, boot_pos: float, lo: float, hi: float, travel_time_s: float, interlock_s: float, now: float):
        self.pos = float(boot_pos)
        self.lo, self.hi = lo, hi
        self.travel = max(0.0, travel_time_s)
        self.interlock = max(0.0, interlock_s)
        self.target = None
        self.up = False
        self.down = False
        self._off_since = {"up": float("-inf"), "down": float("-inf")}
        self._last = now

    @property
    def position(self) -> int:
        return int(round(self.pos))

    @property
    def moving(self) -> bool:
        return self.up or self.down

    @property
    def busy(self) -> bool:
        return self.target is not None

    def set_target(self, target: float, now: float) -> None:
        self.update(now)          # settle motion up to this instant first
        self.target = float(target)
        self.update(now)

    def _relay(self, name: str, on: bool, now: float) -> None:
        if getattr(self, name) and not on:
            self._off_since[name] = now
        setattr(self, name, on)

    def update(self, now: float) -> None:
        dt = max(0.0, now - self._last)
        self._last = now
        if self.target is None:
            self._relay("up", False, now)
            self._relay("down", False, now)
            return
        if self.travel == 0:
            self.pos = self.target
        was = ("up" if self.up else "down") if self.moving else None
        d = 0 if abs(self.target - self.pos) < self.EPS else (1 if self.target > self.pos else -1)
        if d == 0:
            self._finish(now)
            return
        want, other = ("up", "down") if d > 0 else ("down", "up")
        self._relay(other, False, now)                      # interlock: opposite relay always drops first
        if was != want:
            if now - self._off_since[other] < self.interlock:
                self._relay(want, False, now)               # dead time, motor stays off
                return
            self._relay(want, True, now)
            return                                          # motion counts from the next update
        speed = 100.0 / self.travel
        self.pos += d * speed * dt
        self.pos = min(100.0, max(0.0, self.pos))
        if (d > 0 and self.pos >= self.target - self.EPS) or (d < 0 and self.pos <= self.target + self.EPS):
            self.pos = self.target
            self._finish(now)

    def _finish(self, now: float) -> None:
        self.target = None
        self._relay("up", False, now)
        self._relay("down", False, now)
