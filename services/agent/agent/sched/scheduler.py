"""The scheduler: picks the cheapest sufficient route for a turn and records one trace per turn.

    1. fast path   deterministic resolver (no LLM)      -> executor.run_calls   (flag `fast_path`)
    2. router      perception questions, cost ladder    -> read_sensor via the executor, or a vision backend  (flag `router`)
    3. LLM         the planner loop in `ChatService`    (always the last rung; this module only returns None for it)

Everything that touches a device goes through `Executor.run_calls` / `run_call`: the same validation against the live
registry, the same budgets, `needs_confirmation` policy, gateway-minted `confirmation_token` handling (the agent recomputes
the action hash), verify / settle_s handling and action log as an LLM plan. Nothing here can add a tool: the five tools are
fixed in `hub_client.TOOLS`, and the router's sensor source is `read_sensor`."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..devices import DeviceRegistry
from ..events import Event, EventBus
from ..executor import CallOutcome, Executor
from ..reqctx import ReqCtx
from ..store import Store
from .config import SchedConfig
from .grammar import ConstraintCache, ConstraintSchema, make_backend
from .memory import MemoryWindow
from .prefix import PrefixTracker
from .resolver import DENIED, PLAN, FastPathResolver
from .router import CostRouter
from .trace import TRACE_EVENT, TurnTrace

log = logging.getLogger("agent.sched")

GUEST_DENIED_TEXT = "Guests can't control devices."


@dataclass
class Routed:
    outs: list = field(default_factory=list)
    say: str = ""
    text: str | None = None            # a complete answer (skips compose_final), e.g. a sensor reading or a denial
    sse_route: str = "fast"            # fast | vision  (the SSE `route` enum is fast|llm|vision)


class Scheduler:
    def __init__(self, cfg: SchedConfig, registry: DeviceRegistry, executor: Executor | None, store: Store, bus: EventBus,
                 scene_names=lambda: ()) -> None:
        self.cfg, self.registry, self.executor, self.store, self.bus = cfg, registry, executor, store, bus
        self.scene_names = scene_names
        self.resolver = FastPathResolver(cfg)
        self.router = CostRouter(cfg)
        self.constraints = ConstraintCache()
        self.memory = MemoryWindow(store, cfg.window_tokens, cfg.summary_tokens, max_messages=cfg.window_messages)
        self.prefix = PrefixTracker()
        self.by_route: dict[str, dict] = {}

    # ------------------------------------------------------------------------------------------ flags
    def set_flags(self, **flags: bool) -> None:
        for k, v in flags.items():
            if k not in SchedConfig.ABLATION or not isinstance(v, bool):
                raise ValueError(f"not an ablation flag: {k}")
            setattr(self.cfg, k, v)

    def schema(self) -> ConstraintSchema:
        return self.constraints.get(self.registry, tuple(self.scene_names()))

    def new_trace(self, ctx: ReqCtx) -> TurnTrace:
        return TurnTrace(ctx.req_id, flags=self.cfg.flags())

    # ------------------------------------------------------------------------------------------ routing
    async def route(self, ctx: ReqCtx, text: str, trace: TurnTrace) -> Routed | None:
        """A finished non-LLM answer, or None when the LLM must take the turn."""
        if self.executor is None:
            return None
        if self.cfg.fast_path:
            r = await self._fast(ctx, text, trace)
            if r is not None:
                return r
        if self.cfg.router:
            return await self._perception(ctx, text, trace)
        return None

    async def _fast(self, ctx: ReqCtx, text: str, trace: TurnTrace) -> Routed | None:
        res = self.resolver.resolve(text, self.registry, ctx.role, tuple(self.scene_names()))
        trace.fast_reason, trace.confidence = res.reason, round(res.confidence, 3)
        trace.candidates = [list(c) for c in res.candidates]
        if res.status == DENIED:
            self.store.log_action(user_id=ctx.user_id, req_id=ctx.req_id, trigger_kind=ctx.trigger.kind, trigger_ref=None,
                                  reason="fast path: guest may not control devices", tool="set_state", args=None,
                                  status="denied_guest", ok=False, detail={"candidates": trace.candidates})
            trace.route, trace.sse_route, trace.source, trace.outcome = "denied", "fast", "fast_path", "denied"
            return Routed(text=GUEST_DENIED_TEXT)
        if res.status != PLAN:
            return None
        ctx.reason = f"user request (fast path): {text[:80]}"
        outs = await self.executor.run_calls(ctx, res.calls)
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        return Routed(outs=outs, say=res.say)

    async def _perception(self, ctx: ReqCtx, text: str, trace: TurnTrace) -> Routed | None:
        dec = self.router.route(text, self.registry, has_image=ctx.images > 0, role=ctx.role)
        if dec.kind == "other":
            return None
        trace.candidates = [c.__dict__ for c in dec.candidates]
        for src in dec.ladder:
            if src == "llm":
                break
            t0 = time.monotonic()
            if src == "sensor":
                ep, cap = dec.sensor
                out = await self.executor.run_call(ctx, "read_sensor", {"endpoint_id": ep, "capability": cap})
                ms = (time.monotonic() - t0) * 1000
                ok = out.ok and out.result.get("value") is not None
                self.router.observe(src, ms, ok)
                if ok:
                    trace.route, trace.sse_route, trace.source = "sensor", "fast", "sensor"
                    return Routed(outs=[out], text=self._sensor_text(out))
                trace.fallbacks.append(src)
                continue
            backend = next((b for b in self.router.avail.backends if b.supports(src)), None)
            if backend is None:
                continue
            ans = await backend.answer(src, text, [])           # image bytes are not kept past the API layer (gap)
            self.router.observe(src, ans.latency_ms or (time.monotonic() - t0) * 1000, ans.ok)
            if ans.ok and ans.text:
                trace.route, trace.sse_route, trace.source = src.split("_")[0], "vision", src
                return Routed(text=ans.text, sse_route="vision")
            trace.fallbacks.append(src)
        return None

    def _sensor_text(self, out: CallOutcome) -> str:
        r = out.result
        d = self.registry.devices.get(r.get("endpoint_id"))
        val = r.get("value")
        shown = ("yes" if val else "no") if isinstance(val, bool) else val
        unit = f" {r['unit']}" if r.get("unit") and not isinstance(val, bool) else ""
        who = f"{d.name} in {d.room}" if d else "That sensor"
        stale = " (the reading may be out of date)" if r.get("stale") else ""
        return f"{who}: {r.get('capability')} is {shown}{unit}{stale}."

    # ------------------------------------------------------------------------------------------ LLM bookkeeping
    def note_llm(self, trace: TurnTrace, prefix_hash: str, timings: dict, constrained: bool) -> None:
        obs = self.prefix.observe(prefix_hash, timings)
        trace.llm_calls += 1
        trace.prefix_hash = prefix_hash
        trace.prompt_tokens += int(timings.get("prompt_n") or 0)
        trace.cached_tokens += int(timings.get("cache_n") or 0)
        trace.generated_tokens += int(timings.get("predicted_n") or 0)
        if trace.cache_hit is None:                      # the first request of the turn decides; totals are in cached_tokens
            trace.cache_hit = obs.cache_hit
        trace.constrained = constrained

    # ------------------------------------------------------------------------------------------ trace out
    def emit(self, ctx: ReqCtx, trace: TurnTrace) -> None:
        if trace._emitted:
            return
        trace._emitted = True
        trace.finish()
        d = trace.as_dict()
        agg = self.by_route.setdefault(trace.route, {"n": 0, "total_ms": 0})
        agg["n"] += 1
        agg["total_ms"] += trace.latency_ms
        self.store.log_action(user_id=ctx.user_id if ctx.trigger.is_user else None, req_id=ctx.req_id,
                              trigger_kind=ctx.trigger.kind, trigger_ref=None, reason=f"route {trace.route}", tool=None,
                              args=None, status="trace", ok=None, detail=d)
        try:
            self.bus.publish(Event(TRACE_EVENT, d, source="sched", user_id=ctx.user_id if ctx.trigger.is_user else None))
        except RuntimeError:                                         # no running loop (sync tests)
            pass

    def stats(self) -> dict:
        return {"flags": self.cfg.flags(), "constraint_backend": self.cfg.constraint_backend,
                "routes": {k: {"n": v["n"], "avg_ms": round(v["total_ms"] / v["n"], 1)} for k, v in self.by_route.items()},
                "costs": self.router.costs.snapshot(),
                "constraint_cache": {"builds": self.constraints.builds, "hits": self.constraints.hits,
                                     "key": (self.constraints.current.key if self.constraints.current else "")},
                "prefix": self.prefix.stats()}
