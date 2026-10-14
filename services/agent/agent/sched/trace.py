"""One trace per turn, for S10. Emitted on the internal event bus as `sched.trace` and written to the action log
(`status: "trace"`, no tool, the trace in `detail`). Never contains the user's text, tool arguments, tokens or secrets: only
route, source ids, endpoint ids, counts and latencies."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

TRACE_EVENT = "sched.trace"


@dataclass
class TurnTrace:
    req_id: str
    route: str = "llm"                       # fast | sensor | yolo | vlm | llm | denied  (the SSE `route` is fast|llm|vision)
    sse_route: str = "llm"
    source: str = ""                         # cost-table source that answered: sensor | yolo_pi | ... | llm | fast_path
    candidates: list = field(default_factory=list)   # fast: [(endpoint|scene, score)]; router: [{source, cost_ms, flag, available, reason}]
    fast_reason: str = ""                    # why the fast path matched / fell through
    confidence: float = 0.0
    latency_ms: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    generated_tokens: int = 0
    cache_hit: bool | None = None            # None = no LLM call this turn
    prefix_hash: str = ""
    constrained: bool = False
    outcome: str = "ok"                      # ok | partial | failed | needs_confirmation | denied | error:<code>
    flags: dict = field(default_factory=dict)
    fallbacks: list = field(default_factory=list)    # sources tried and failed before the final one
    _t0: float = field(default_factory=time.monotonic, repr=False)
    _emitted: bool = field(default=False, repr=False)

    def finish(self) -> "TurnTrace":
        self.latency_ms = int((time.monotonic() - self._t0) * 1000)
        return self

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
