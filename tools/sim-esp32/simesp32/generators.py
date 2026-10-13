"""Sensor value generators. Deterministic for a given seed and time, so tests and demos are repeatable."""
import math
import random

KINDS = ("constant", "sine", "random_walk", "sequence", "random_choice")


class Generator:
    def __init__(self, cap, spec: dict | None, rng: random.Random):
        self.cap = cap
        self.rng = rng
        self.spec = dict(spec or {})
        self.kind = self.spec.get("kind") or self._default_kind()
        if self.kind not in KINDS:
            raise ValueError(f"unknown generator kind '{self.kind}' (one of {KINDS})")
        self._i = 0
        self._walk = self.spec.get("start")
        self._choice = None

    def _default_kind(self):
        return "random_choice" if self.cap.type in ("bool", "enum") else "sine"

    def sample(self, t: float):
        v = getattr(self, "_" + self.kind)(t)
        return self._coerce(v)

    # --- kinds ---
    def _constant(self, t):
        return self.spec.get("value", self._default_value())

    def _sine(self, t):
        mean = self.spec.get("mean", self._default_value())
        amp = self.spec.get("amplitude", 1.0)
        period = self.spec.get("period_s", 600.0)
        noise = self.spec.get("noise", 0.0)
        return mean + amp * math.sin(2 * math.pi * t / period) + (self.rng.gauss(0, noise) if noise else 0.0)

    def _random_walk(self, t):
        step = self.spec.get("step", 0.5)
        if self._walk is None:
            self._walk = self.spec.get("start", self._default_value())
        self._walk += self.rng.uniform(-step, step)
        lo, hi = self.spec.get("min"), self.spec.get("max")
        if lo is not None:
            self._walk = max(lo, self._walk)
        if hi is not None:
            self._walk = min(hi, self._walk)
        return self._walk

    def _sequence(self, t):
        vals = self.spec.get("values")
        if not vals:
            raise ValueError("sequence generator needs values")
        v = vals[self._i % len(vals)]
        self._i += 1
        return v

    def _random_choice(self, t):
        vals = self.spec.get("values")
        if vals is None:
            vals = [False, True] if self.cap.type == "bool" else (self.cap.values or [])
        p = self.spec.get("change_probability", 0.2)
        if self._choice is None:
            self._choice = vals[0]
        elif self.rng.random() < p:
            self._choice = self.rng.choice(vals)
        return self._choice

    # --- helpers ---
    def _default_value(self):
        return {"temperature": 25.0, "humidity": 50.0, "illuminance": 300.0, "power_w": 10.0,
                "current_a": 0.1, "voltage_v": 230.0, "battery": 90}.get(self.cap.id, 0.0)

    def _coerce(self, v):
        c = self.cap
        if c.type == "bool":
            return bool(v)
        if c.type == "enum":
            if v not in (c.values or []):
                raise ValueError(f"generator produced {v!r}, not in {c.values}")
            return v
        lo, hi = c.effective_range()
        if c.type == "int":
            v = int(round(v))
        else:
            v = round(float(v), 2)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v
