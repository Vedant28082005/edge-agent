"""Cost-based router for perception questions (ARCHITECTURE section 5).

    question -> kind -> candidate sources that CAN answer it -> cheapest AVAILABLE one; failures fall up the cost ladder

Sources, cheapest first by the seed table: `sensor` (read_sensor through the hub), `yolo_jetson` / `yolo_pi` (object
presence and counts), `vlm_yesno`, `vlm_short`, `vlm_json`, `llm` (the always-available last resort). The cost table
lives in config (`SchedConfig.costs`, every entry flagged `est` or `measured`) and each observed latency updates an EWMA,
so the order adapts to what the hardware actually does. A source that has just failed is skipped for `breaker_s`; health
reports (`update_health`, fed from `node_health` events) gate availability, e.g. VLM below 400 MB MemAvailable, Jetson
down so YOLO runs on the Pi or is skipped.

No new agent tool is involved: `sensor` is the existing `read_sensor` through the executor; the vision sources are behind
the `PerceptionBackend` interface and are unavailable until a backend is registered (vision-node / yolo-node have no
internal API contract yet, so nothing here calls them directly)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from ..devices import DeviceRegistry
from .config import DEFAULT_COSTS, SchedConfig
from .text import normalise, score_device, stem

SOURCES = ("sensor", "yolo_jetson", "yolo_pi", "vlm_yesno", "vlm_short", "vlm_json", "llm")

# question kinds
SENSOR_Q, PRESENCE_Q, YESNO_Q, OPEN_Q, STRUCT_Q, OTHER_Q = "sensor", "presence", "yesno", "open", "structured", "other"

_SENSOR_WORDS = {  # word -> sensor capability
    "temperature": "temperature", "temp": "temperature", "hot": "temperature", "warm": "temperature", "cold": "temperature",
    "cool": "temperature", "humidity": "humidity", "humid": "humidity", "lux": "illuminance", "illuminance": "illuminance",
    "battery": "battery", "watts": "power_w", "wattage": "power_w", "voltage": "voltage_v",
    "current": "current_a", "motion": "motion", "movement": "motion"}
_CONTACT_WORDS = {"open", "closed", "shut"}
_COCO = {"person", "people", "someone", "anyone", "anybody", "somebody", "man", "woman", "kid", "child", "dog", "cat",
         "car", "bicycle", "bike", "package", "parcel", "bird", "intruder"}
_VISUAL_HINT = {"camera", "see", "image", "picture", "photo", "visible", "looks", "look", "porch", "doorstep", "driveway",
                "garden", "yard", "street", "outside"}
_STRUCT_HINT = {"describe", "list", "json", "everything", "inventory", "summarize", "summary"}
_QUESTION_START = {"is", "are", "was", "what", "whats", "which", "how", "hows", "does", "do", "who", "where", "can", "any", "tell"}


@dataclass
class Candidate:
    source: str
    cost_ms: float
    flag: str
    available: bool
    reason: str = ""


@dataclass
class RouteDecision:
    kind: str
    chosen: str                                   # a source, or "llm"
    candidates: list = field(default_factory=list)    # [Candidate] in ladder order, all considered
    ladder: list = field(default_factory=list)    # the usable sources, cheapest first (chosen first); last is "llm"
    sensor: tuple | None = None                   # (endpoint_id, capability) when the sensor source applies
    detail: str = ""


@dataclass
class BackendAnswer:
    ok: bool
    text: str = ""
    latency_ms: float = 0.0


class PerceptionBackend(Protocol):
    """Implemented by the (future) vision-node / yolo-node clients. `source` is one of the vision sources above."""
    def supports(self, source: str) -> bool: ...
    async def answer(self, source: str, question: str, images: list) -> BackendAnswer: ...


class CostTable:
    """EWMA latency per source, seeded from config. `flag` stays `est` until a real observation arrives, then `measured`
    (an observed latency on this system is a measurement, the seed is not)."""

    def __init__(self, cfg: SchedConfig) -> None:
        self.alpha = cfg.ewma_alpha
        self.ms: dict[str, float] = {k: float(cfg.costs.get(k, DEFAULT_COSTS[k])[0]) for k in SOURCES}
        self.flag: dict[str, str] = {k: str(cfg.costs.get(k, DEFAULT_COSTS[k])[1]) for k in SOURCES}
        self.n: dict[str, int] = {k: 0 for k in SOURCES}

    def observe(self, source: str, latency_ms: float) -> None:
        if source not in self.ms or latency_ms < 0:
            return
        self.ms[source] = self.alpha * latency_ms + (1 - self.alpha) * self.ms[source] if self.n[source] else float(latency_ms)
        self.n[source] += 1
        self.flag[source] = "measured"

    def snapshot(self) -> dict:
        return {k: {"ms": round(self.ms[k], 1), "flag": self.flag[k], "n": self.n[k]} for k in SOURCES}


class Availability:
    """Which sources may be used right now: a failure breaker, node health, and the registered backends."""

    def __init__(self, cfg: SchedConfig, clock=time.monotonic) -> None:
        self.cfg, self.clock = cfg, clock
        self.backends: list[PerceptionBackend] = []
        self.health: dict[str, dict] = {}              # node name -> {"up": bool, "mem_available_mb": float}
        self._down_until: dict[str, float] = {}

    def register(self, backend: PerceptionBackend) -> None:
        self.backends.append(backend)

    def update_health(self, node: str, up: bool, mem_available_mb: float | None = None) -> None:
        self.health[node] = {"up": bool(up), "mem_available_mb": mem_available_mb}

    def failed(self, source: str) -> None:
        self._down_until[source] = self.clock() + self.cfg.breaker_s

    def recovered(self, source: str) -> None:
        self._down_until.pop(source, None)

    def check(self, source: str, has_image: bool) -> tuple[bool, str]:
        if source == "llm":
            return True, ""
        if self.clock() < self._down_until.get(source, 0.0):
            return False, "recent_failure"
        if source == "sensor":
            return True, ""
        if not any(b.supports(source) for b in self.backends):
            return False, "no_backend"
        if not has_image:
            return False, "no_image_or_camera"
        node = {"yolo_jetson": "jetson-yolo", "yolo_pi": "pi-vision"}.get(source, "pi-vision")
        h = self.health.get(node)
        if h is not None and not h["up"]:
            return False, "node_down"
        if source.startswith("vlm") and h is not None and h.get("mem_available_mb") is not None \
                and h["mem_available_mb"] < self.cfg.min_vlm_mem_mb:
            return False, "low_memory"
        return True, ""


# --------------------------------------------------------------------------------------------- question analysis
def classify(text: str) -> str:
    s = normalise(text)
    toks = set(s.split())
    stems = {stem(t) for t in toks}
    if not toks:
        return OTHER_Q
    if toks & _STRUCT_HINT and (toks & _VISUAL_HINT or toks & _COCO):
        return STRUCT_Q
    asks = text.strip().endswith("?") or s.split()[0] in _QUESTION_START
    if not asks:
        return OTHER_Q
    if toks & _COCO or stems & _COCO:
        return PRESENCE_Q
    if toks & set(_SENSOR_WORDS) or (toks & _CONTACT_WORDS):
        return SENSOR_Q
    if toks & _VISUAL_HINT:
        return YESNO_Q if s.split()[0] in ("is", "are", "was", "does", "do", "can", "any") else OPEN_Q
    return OTHER_Q


def find_sensor(text: str, reg: DeviceRegistry) -> tuple[tuple[str, str] | None, str]:
    """(endpoint_id, capability) of the one sensor the question is about, or (None, reason)."""
    s = normalise(text)
    toks = s.split()
    cap = next((_SENSOR_WORDS[t] for t in toks if t in _SENSOR_WORDS), None)
    if cap is None and set(toks) & _CONTACT_WORDS:
        cap = "contact"
    if cap is None:
        return None, "no_capability_word"
    drop = set(_SENSOR_WORDS) | _CONTACT_WORDS | {"is", "are", "it", "what", "whats", "how", "the", "in", "of", "at", "now",
                                                   "currently", "right", "reading", "level", "much", "many", "there", "any",
                                                   "still", "was", "does", "do", "tell", "me", "a", "an", "my", "on", "to"}
    xs = [stem(t) for t in toks if t not in drop]
    cands = []
    for d in reg.devices.values():
        c = d.cap(cap)
        if c is None or c.kind != "sensor":
            continue
        sc, ident = score_device(xs, d) if xs else (0.0, False)
        cands.append((sc if xs else 0.5, d))
    if not cands:
        return None, "no_sensor_with_capability"
    cands.sort(key=lambda t: (-t[0], t[1].endpoint_id))
    if len(cands) == 1 and (not xs or cands[0][0] >= 0.5):
        return (cands[0][1].endpoint_id, cap), "single_sensor"
    if cands[0][0] < 0.85 or (len(cands) > 1 and cands[1][0] >= cands[0][0] - 0.15):
        return None, "ambiguous_sensor"
    return (cands[0][1].endpoint_id, cap), "matched_sensor"


class CostRouter:
    def __init__(self, cfg: SchedConfig | None = None, availability: Availability | None = None) -> None:
        self.cfg = cfg or SchedConfig()
        self.costs = CostTable(self.cfg)
        self.avail = availability or Availability(self.cfg)

    def route(self, text: str, reg: DeviceRegistry, has_image: bool = False, role: str = "member") -> RouteDecision:
        kind = classify(text)
        if kind == OTHER_Q:
            return RouteDecision(kind, "llm", ladder=["llm"], detail="not a perception question")
        sensor, sdetail = (None, "")
        wants: list[str] = []
        if kind == SENSOR_Q:
            sensor, sdetail = find_sensor(text, reg)
            if sensor is not None:
                wants.append("sensor")
        elif kind == PRESENCE_Q:
            wants += ["yolo_jetson", "yolo_pi", "vlm_yesno", "vlm_short"]
        elif kind == YESNO_Q:
            wants += ["vlm_yesno", "vlm_short"]
        elif kind == OPEN_Q:
            wants += ["vlm_short", "vlm_json"]
        elif kind == STRUCT_Q:
            wants += ["vlm_json"]
        wants.append("llm")
        cands, usable = [], []
        for src in sorted(dict.fromkeys(wants), key=lambda x: self.costs.ms[x]):   # stable: ties keep the order above
            ok, why = self.avail.check(src, has_image)
            cands.append(Candidate(src, round(self.costs.ms[src], 1), self.costs.flag[src], ok, why))
            if ok:
                usable.append(src)
        if "llm" not in usable:
            usable.append("llm")
        return RouteDecision(kind, usable[0], cands, usable, sensor, sdetail)

    def observe(self, source: str, latency_ms: float, ok: bool) -> None:
        if ok:
            self.costs.observe(source, latency_ms)
            self.avail.recovered(source)
        elif source != "llm":
            self.avail.failed(source)
