"""Scheduler configuration. Lives under `AgentConfig.sched` (a JSON object in agent.json); unknown keys are rejected.

The four ablation flags can be flipped at runtime (`POST /v1/runtime`) so S10 can run every combination without a
restart. Cost numbers are the seed of the router's cost table: `flag` is `est` or `measured` and is carried into every
trace and report line (CLAUDE.md rule 7)."""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field, fields
from urllib.parse import urlsplit

# source -> (latency_ms, flag). Seeds from the owner's brief. YOLOv8n 94 ms and VLM yes/no 2.4 s are [measured]; the
# VLM short-answer seed (5.5 s) is UNVERIFIED until the owner confirms it (kept as `est`); the LLM turn stays [est].
DEFAULT_COSTS: dict[str, tuple[float, str]] = {
    "sensor": (5.0, "est"),                 # read_sensor through the hub: milliseconds
    "yolo_jetson": (94.0, "measured"),      # YOLOv8n 94 ms [measured]
    "yolo_pi": (94.0, "measured"),          # YOLOv8n 94 ms [measured]
    "vlm_yesno": (2400.0, "measured"),       # VLM yes/no 2.4 s [measured]
    "vlm_short": (5500.0, "est"),            # UNVERIFIED seed: do not report as measured until the owner confirms
    "vlm_json": (13500.0, "est"),           # bounded JSON, 12-15 s
    "llm": (22500.0, "est"),                # one LLM turn, 15-30 s
}


# Perception nodes (sched/nodes.py). ALL DISABLED by default: perception is optional. `secret_file` is a path, never a secret;
# one secret per node (vision-node-api.secret / yolo-node-api.secret), different from the hub secret and from each other.
# The contracts specify plain HTTP over Tailscale (WireGuard); an https `base_url` plus `ca_file` is also accepted.
DEFAULT_NODES: dict[str, dict] = {
    "vision": {"enabled": False, "base_url": "http://pi-vision:8300",
               "secret_file": "/etc/edge-agent/secrets/vision-node-api.secret", "timeout_s": 30.0, "ca_file": None, "min_conf": 0.25},
    "yolo": {"enabled": False, "base_url": "http://jetson-yolo:8400",
             "secret_file": "/etc/edge-agent/secrets/yolo-node-api.secret", "timeout_s": 10.0, "ca_file": None, "min_conf": 0.25,
             "snapshot_sources": []},                 # [{"source_id": "cam0", "room": "Hall"}]: which room each camera watches
}
_SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def normalise_nodes(data: dict | None) -> dict:
    """Defaults merged with the user's per-node overrides; unknown nodes or keys and malformed values are rejected."""
    out = deepcopy(DEFAULT_NODES)
    if not isinstance(data, dict):
        raise ValueError("sched.nodes must be an object")
    for name, over in data.items():
        if name not in out or not isinstance(over, dict):
            raise ValueError(f"unknown sched node: {name}")
        for k, v in over.items():
            if k not in out[name]:
                raise ValueError(f"unknown sched.nodes.{name} key: {k}")
            out[name][k] = v
    for name, n in out.items():
        u = urlsplit(n["base_url"]) if isinstance(n["base_url"], str) else None
        if u is None or u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError(f"sched.nodes.{name}.base_url must be a plain http(s) URL without credentials")
        if not isinstance(n["enabled"], bool) or not isinstance(n["secret_file"], str) or not n["secret_file"]:
            raise ValueError(f"sched.nodes.{name}: bad enabled or secret_file")
        if isinstance(n["timeout_s"], bool) or not isinstance(n["timeout_s"], (int, float)) or not 1 <= n["timeout_s"] <= 60:
            raise ValueError(f"sched.nodes.{name}.timeout_s must be 1..60")
        if isinstance(n["min_conf"], bool) or not isinstance(n["min_conf"], (int, float)) or not 0.05 <= n["min_conf"] <= 1:
            raise ValueError(f"sched.nodes.{name}.min_conf must be 0.05..1")
        if n["ca_file"] is not None and not isinstance(n["ca_file"], str):
            raise ValueError(f"sched.nodes.{name}.ca_file must be a path or null")
        for s in n.get("snapshot_sources", []):
            if not (isinstance(s, dict) and set(s) == {"source_id", "room"} and isinstance(s["source_id"], str)
                    and _SOURCE_ID.fullmatch(s["source_id"]) and isinstance(s["room"], str) and s["room"].strip()):
                raise ValueError(f"bad sched.nodes.{name}.snapshot_sources entry")
        if len(n.get("snapshot_sources", [])) > 8:
            raise ValueError(f"sched.nodes.{name}.snapshot_sources: at most 8")
    return out


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
    # perception nodes (disabled by default)
    nodes: dict = field(default_factory=lambda: deepcopy(DEFAULT_NODES))

    @classmethod
    def from_dict(cls, data: dict | None) -> "SchedConfig":
        cfg = cls()
        known = {f.name for f in fields(cls)}
        for k, v in (data or {}).items():
            if k not in known:
                raise ValueError(f"unknown sched config key: {k}")
            setattr(cfg, k, v)
        cfg.nodes = normalise_nodes(cfg.nodes)
        if cfg.constraint_backend not in ("json_schema", "gbnf"):
            raise ValueError("sched.constraint_backend must be json_schema or gbnf")
        for k, v in cfg.costs.items():
            if k not in DEFAULT_COSTS or not (isinstance(v, (list, tuple)) and len(v) == 2 and v[1] in ("est", "measured")):
                raise ValueError(f"bad sched cost entry: {k}")
        return cfg

    ABLATION = ("fast_path", "router", "constrained_decoding", "prefix_cache")

    def flags(self) -> dict:
        return {k: getattr(self, k) for k in self.ABLATION}
