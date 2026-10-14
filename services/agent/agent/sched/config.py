"""Scheduler configuration. Lives under `AgentConfig.sched` (a JSON object in agent.json); unknown keys are rejected.

The four ablation flags can be flipped at runtime (`POST /v1/runtime`) so S10 can run every combination without a
restart. Cost numbers are the seed of the router's cost table: `flag` is `est` or `measured` and is carried into every
trace and report line (CLAUDE.md rule 7)."""
from __future__ import annotations

from dataclasses import dataclass, field, fields

# source -> (latency_ms, flag). Seeds from the owner's brief; ARCHITECTURE section 5 records the 94 ms YOLOv8n figure as
# measured on the Pi and the VLM short answer (5.5 s) as measured; they stay `est` here until re-measured by S10.
DEFAULT_COSTS: dict[str, tuple[float, str]] = {
    "sensor": (5.0, "est"),                 # read_sensor through the hub: milliseconds
    "yolo_jetson": (94.0, "est"),           # YOLOv8n TensorRT; Jetson not yet measured
    "yolo_pi": (94.0, "est"),               # YOLOv8n on the vision Pi
    "vlm_yesno": (2400.0, "est"),
    "vlm_short": (5500.0, "est"),
    "vlm_json": (13500.0, "est"),           # bounded JSON, 12-15 s
    "llm": (22500.0, "est"),                # one LLM turn, 15-30 s
}


@dataclass
class SchedConfig:
    # ablation switches
    fast_path: bool = True
    router: bool = True
    constrained_decoding: bool = True
    prefix_cache: bool = True
    # fast path
    fast_threshold: float = 0.85            # minimum confidence for a plan; below it the LLM gets the turn
    fast_margin: float = 0.15               # the best candidate must beat the runner-up by this much
    fast_max_group: int = 8                 # "all lights in X" never expands beyond this many calls
    # router
    ewma_alpha: float = 0.3
    breaker_s: float = 30.0                 # a source that just failed is skipped this long
    min_vlm_mem_mb: float = 400.0           # ARCHITECTURE section 5: VLM rejected below 400 MB MemAvailable
    costs: dict = field(default_factory=lambda: {k: [v[0], v[1]] for k, v in DEFAULT_COSTS.items()})
    # constrained decoding
    constraint_backend: str = "json_schema"  # json_schema (response_format) | gbnf (grammar field)
    # prefix cache
    id_slot: int = 0
    # memory
    window_messages: int = 4                # the prompt builder keeps the last 4 messages (1-2 turns) at most
    window_tokens: int = 400                # recent turns (summary excluded), est tokens
    summary_tokens: int = 100

    @classmethod
    def from_dict(cls, data: dict | None) -> "SchedConfig":
        cfg = cls()
        known = {f.name for f in fields(cls)}
        for k, v in (data or {}).items():
            if k not in known:
                raise ValueError(f"unknown sched config key: {k}")
            setattr(cfg, k, v)
        if cfg.constraint_backend not in ("json_schema", "gbnf"):
            raise ValueError("sched.constraint_backend must be json_schema or gbnf")
        for k, v in cfg.costs.items():
            if k not in DEFAULT_COSTS or not (isinstance(v, (list, tuple)) and len(v) == 2 and v[1] in ("est", "measured")):
                raise ValueError(f"bad sched cost entry: {k}")
        return cfg

    ABLATION = ("fast_path", "router", "constrained_decoding", "prefix_cache")

    def flags(self) -> dict:
        return {k: getattr(self, k) for k in self.ABLATION}
