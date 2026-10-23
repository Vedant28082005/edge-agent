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
from ..scope import UNRESTRICTED, Scope
from ..store import Store
from ..views import ScopedViews
from .config import SchedConfig
from .grammar import ConstraintCache, ConstraintSchema, make_backend
from .memory import MemoryWindow
from .prefix import PrefixTracker
from .resolver import DENIED, PLAN, FastPathResolver
from .router import CostRouter
from .trace import RAW_TRUNC, TRACE_EVENT, TurnTrace

log = logging.getLogger("agent.sched")

GUEST_DENIED_TEXT = "Guests can't control devices."


@dataclass
class Routed:
    outs: list = field(default_factory=list)
    say: str = ""
    text: str | None = None            # a complete answer (skips compose_final), e.g. a sensor reading or a denial
    sse_route: str = "fast"            # fast | sensor | yolo | vlm  (SSE `route` enum fast|llm|sensor|yolo|vlm; `vision` is legacy, never emitted)


class Scheduler:
    def __init__(self, cfg: SchedConfig, registry: DeviceRegistry, executor: Executor | None, store: Store, bus: EventBus,
                 views: ScopedViews | None = None) -> None:
        self.cfg, self.registry, self.executor, self.store, self.bus = cfg, registry, executor, store, bus
        self.views = views or ScopedViews(registry)
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

    def schema(self, scope: Scope = UNRESTRICTED) -> ConstraintSchema:
        """Decoding schema for this scope: the full registry for an unrestricted caller, otherwise only its own endpoints,
        rooms and scenes. Keyed on the hub's `registry_version` (content hash when the hub sent none)."""
        return self.constraints.get(self.views.get(scope).registry, None, "" if not scope.restricted else scope.key)

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
        reg = self.views.get(ctx.scope).registry          # scoped: the resolver cannot even name an out-of-scope device or scene
        res = self.resolver.resolve(text, reg, ctx.role, reg.scene_names())
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
        snap = self._snapshot_ref(ctx, text)
        dec = self.router.route(text, self.views.get(ctx.scope).registry, has_image=ctx.images > 0, role=ctx.role,
                                can_snapshot=snap is not None)
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
                    trace.route, trace.sse_route, trace.source = "sensor", "sensor", "sensor"
                    return Routed(outs=[out], text=self._sensor_text(out), sse_route="sensor")
                trace.fallbacks.append(src)
                continue
            backend = next((b for b in self.router.avail.backends if b.supports(src)), None)
            if backend is None:
                continue
            # the caller's own images (RAM only, released when the turn ends); else, for yolo_jetson alone, the camera snapshot
            # that `_snapshot_ref` authorised for this role and scope. Never both, never a snapshot for another source.
            imgs = list(ctx.image_data) or ([snap] if snap is not None and src == "yolo_jetson" else [])
            ans = await backend.answer(src, text, imgs)
            if ans.busy:                                         # queue_full / camera unusable: not a failure, try the next rung
                trace.fallbacks.append(f"{src}:busy")
                continue
            self.router.observe(src, ans.latency_ms or (time.monotonic() - t0) * 1000, ans.ok)
            if ans.ok and ans.text:
                kind = src.split("_")[0]                                  # yolo | vlm
                trace.route, trace.sse_route, trace.source = kind, kind, src
                return Routed(text=ans.text, sse_route=kind)
            trace.fallbacks.append(src)
        return None

    def _snapshot_ref(self, ctx: ReqCtx, text: str):
        """A camera snapshot the yolo backend allows THIS caller to take (role and room checked there), only when no image is
        attached. Guests never; any backend without the check, or any doubt, means None (fail closed)."""
        if ctx.image_data or ctx.images > 0 or ctx.role == "guest" or ctx.trigger.guest:
            return None
        for b in self.router.avail.backends:
            check = getattr(b, "snapshot_source", None)
            if check is not None and b.supports("yolo_jetson"):
                return check(ctx.role, ctx.scope, text)
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
    def note_llm(self, trace: TurnTrace, prefix_hash: str, timings: dict, constrained: bool, raw: str = "") -> None:
        obs = self.prefix.observe(prefix_hash, timings)
        trace.llm_calls += 1
        trace.prefix_hash = prefix_hash
        trace.prompt_tokens += int(timings.get("prompt_n") or 0)
        trace.cached_tokens += int(timings.get("cache_n") or 0)
        trace.generated_tokens += int(timings.get("predicted_n") or 0)
        if trace.cache_hit is None:                      # the first request of the turn decides; totals are in cached_tokens
            trace.cache_hit = obs.cache_hit
        trace.constrained = constrained
        trace.llm_raw = (raw or "")[:RAW_TRUNC]

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
        self.store.log_trace(user_id=ctx.user_id if ctx.trigger.is_user else None, req_id=ctx.req_id,
                             route=trace.route, detail=d)
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
