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
from ..render import LIST_MAX, ambient_clarifying_text, describe_outcomes, describe_question, describe_reads
from ..util import clean_label
from ..reqctx import ReqCtx
from ..schema import (ambient_default_call, ambient_domain_devices, ambient_request_signal, ensure_power_dependency,
                      validate_call)
from ..scope import UNRESTRICTED, Scope
from ..store import Store
from ..views import ScopedViews
from .config import SchedConfig
from .intents import is_identity_intent, is_list_intent
from .grammar import ConstraintCache, ConstraintSchema, make_backend
from .memory import MemoryWindow
from .prefix import PrefixTracker
from .resolver import DENIED, PLAN, RELATIVE_PENDING, FastPathResolver
from .router import CostRouter
from .trace import RAW_TRUNC, TRACE_EVENT, TurnTrace

log = logging.getLogger("agent.sched")

GUEST_DENIED_TEXT = "Guests can't control devices."
IDENTITY_TEXT = ("I'm your smart-home assistant: I can control your lights, fans, locks and other devices, run scenes, "
                 "and tell you what's on.")


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
        self.model_name = None             # async () -> str | None: the LLM's name (config, else llama-server /props); set by Agent

    # ------------------------------------------------------------------------------------------ flags
    def set_flags(self, **flags: bool) -> None:
        for k, v in flags.items():
            if k not in SchedConfig.ABLATION or not isinstance(v, bool):
                raise ValueError(f"not an ablation flag: {k}")
            setattr(self.cfg, k, v)

    def schema(self, scope: Scope = UNRESTRICTED) -> ConstraintSchema:
        """Decoding schema for this scope: the full registry for an unrestricted caller, otherwise only its own endpoints,
        rooms and scenes. Keyed on the hub's `registry_version` (content hash when the hub sent none)."""
        return self.constraints.get(self.views.get(scope).registry, None, "" if not scope.restricted else scope.key,
                                    self.cfg.say_max_tokens if self.cfg.allow_say else 0)

    def aliases(self, scope: Scope = UNRESTRICTED):
        """(S8i) The same scope's alias map, from the same cache entry as `schema` -- so the planner's decode and the
        prompt's device list always agree on what e.g. "e3" means."""
        return self.schema(scope).aliases

    def new_trace(self, ctx: ReqCtx) -> TurnTrace:
        return TurnTrace(ctx.req_id, flags=self.cfg.flags(), route_mode=self.cfg.route_mode)

    # ------------------------------------------------------------------------------------------ routing
    async def route(self, ctx: ReqCtx, text: str, trace: TurnTrace) -> Routed | None:
        """A finished non-LLM answer, or None when the LLM must take the turn."""
        if is_identity_intent(text) and self.cfg.allows_fast("identity"):   # a fixed capability line: never a device read
            return await self._identity(trace)
        if self.executor is None:
            return None
        if self.cfg.fast_path and self.cfg.route_mode != "llm_first":
            r = await self._fast(ctx, text, trace)
            if r is not None:
                return r
        if self.cfg.router:
            return await self._perception(ctx, text, trace)
        return None

    async def _fast(self, ctx: ReqCtx, text: str, trace: TurnTrace) -> Routed | None:
        reg = self.views.get(ctx.scope).registry          # scoped: the resolver cannot even name an out-of-scope device or scene
        allow = self.cfg.allows_fast                       # route_mode / fast_intents: an intent not allowed goes to the LLM
        if is_list_intent(text):
            return await self._list(ctx, trace, reg) if allow("list") else None
        status = self.resolver.resolve_status(text, reg)
        if status is not None:
            return await self._status(ctx, status, trace, reg) if allow("status") else None
        question = self.resolver.resolve_question(text, reg)
        if question is not None:
            return await self._question(ctx, question, trace, reg) if allow("status") else None
        res = self.resolver.resolve(text, reg, ctx.role, reg.scene_names())
        if res.status in (PLAN, DENIED, RELATIVE_PENDING) and not allow(self._intent_of(res)):
            trace.fast_reason = f"intent_{self._intent_of(res)}_to_llm"
            return None
        trace.fast_reason, trace.confidence = res.reason, round(res.confidence, 3)
        trace.candidates = [list(c) for c in res.candidates]
        if res.status == DENIED:
            self.store.log_action(user_id=ctx.user_id, req_id=ctx.req_id, trigger_kind=ctx.trigger.kind, trigger_ref=None,
                                  reason="fast path: guest may not control devices", tool="set_state", args=None,
                                  status="denied_guest", ok=False, detail={"candidates": trace.candidates})
            trace.route, trace.sse_route, trace.source, trace.outcome = "denied", "fast", "fast_path", "denied"
            return Routed(text=GUEST_DENIED_TEXT)
        if res.status == RELATIVE_PENDING:
            r = await self._relative(ctx, res, trace)
            if r is not None:
                return r
            trace.fast_reason = "relative_state_unavailable"
            return None                                     # cannot compute a target safely: let the LLM take it
        if res.status != PLAN:
            if res.unknown and ctx.role != "guest" and allow("command"):
                trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
                return Routed(text=self._unknown_text(res.unknown, reg))
            if res.reason == "relative":
                return None      # an explicit relative command word (e.g. "brighter") overlapping an ambient word
                                 # ("too bright") is a real command the resolver already recognised, never a complaint
            return await self._ambient(ctx, text, trace, reg) if allow("ambient") else None
        ctx.reason = f"user request (fast path): {text[:80]}"
        trace.note_calls(res.calls)
        outs = await self.executor.run_calls(ctx, res.calls)
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        return Routed(outs=outs, say=res.say)

    @staticmethod
    def _intent_of(res) -> str:
        """The `fast_intents` name of a resolver result: compound | set_value | relative | command."""
        if res.reason == "compound_power_value":
            return "compound"
        if res.status == RELATIVE_PENDING or res.intent == "relative":
            return "relative"
        return "set_value" if res.intent in ("value", "mode") else "command"

    @staticmethod
    def _unknown_text(name: str, reg: DeviceRegistry) -> str:
        """"I don't see a kitchen fan. I can control: Test light." -- real device names only, never acts on a guess."""
        names = sorted({d.name for d in reg.devices.values() if any(c.kind == "actuator" for c in d.caps)})
        art = "an" if name[:1] in "aeiou" else "a"
        if not names:
            return f"I don't see {art} {name}."
        more = f" and {len(names) - 5} more" if len(names) > 5 else ""
        return f"I don't see {art} {name}. I can control: {', '.join(names[:5])}{more}."

    async def _identity(self, trace: TurnTrace) -> Routed:
        """"who are you" / "what is the llm name": the fixed capability line plus the model's name when it is known."""
        name = ""
        if self.model_name is not None:
            try:
                name = clean_label(await self.model_name(), 48)
            except Exception:                                  # a failed /props lookup only drops the name
                log.warning("model name lookup failed", exc_info=True)
        trace.fast_reason, trace.confidence = "identity", 1.0
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        return Routed(text=f"{IDENTITY_TEXT} I run on {name}." if name else IDENTITY_TEXT)

    async def _list(self, ctx: ReqCtx, trace: TurnTrace, reg: DeviceRegistry) -> Routed:
        """"devices" / "list devices" / "what can you control": `list_devices`, then one `get_state` per room of the (at most
        `LIST_MAX`) devices shown so the reply can say on/off and the level. No LLM."""
        trace.fast_reason, trace.confidence = "list_devices", 1.0
        calls: list[dict] = [{"tool": "list_devices", "args": {}}]
        shown = sorted(reg.devices.values(), key=lambda d: (d.room.lower(), d.name.lower(), d.endpoint_id))[:LIST_MAX]
        for room in dict.fromkeys(d.room for d in shown):
            calls.append({"tool": "get_state", "args": {"room": room}})
        trace.note_calls(calls)
        outs = await self.executor.run_calls(ctx, calls)
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        return Routed(outs=outs, text=describe_reads(outs, reg) or "I don't have a device list yet.")

    async def _status(self, ctx: ReqCtx, status, trace: TurnTrace, reg: DeviceRegistry) -> Routed:
        """(S8j) "what is the device status?" -- a question, answered with get_state and no LLM, never a write."""
        trace.fast_reason, trace.confidence = status.reason, round(status.confidence, 3)
        trace.candidates = [list(c) for c in status.candidates]
        trace.note_calls(status.calls)
        outs = await self.executor.run_calls(ctx, status.calls)
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        return Routed(outs=outs, text=describe_reads(outs, reg) or "I don't have a reading for that yet.")

    async def _question(self, ctx: ReqCtx, res, trace: TurnTrace, reg: DeviceRegistry) -> Routed:
        """(S8l) "is the ceiling light on?" / "how bright is the lamp?" / "is anything on?" -- a yes/no or
        single-capability question, answered with get_state and no LLM, never a write."""
        trace.fast_reason, trace.confidence = res.reason, round(res.confidence, 3)
        trace.candidates = [list(c) for c in res.candidates]
        trace.note_calls(res.calls)
        outs = await self.executor.run_calls(ctx, res.calls)
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        text = describe_question(outs, reg, res.question) if res.question else ""
        return Routed(outs=outs, text=text or "I don't have a reading for that yet.")

    async def _ambient(self, ctx: ReqCtx, text: str, trace: TurnTrace, reg: DeviceRegistry) -> Routed | None:
        """(S8j) "it's dark/bright/hot/cold/stuffy in here", no device named: act without the LLM when exactly one
        device in that domain can answer and has the capability the direction needs; ask (naming real devices) when
        more than one could; None (fall through to the router/LLM) for anything else, including a domain no device
        in scope can answer at all."""
        sig = ambient_request_signal(text, reg)
        if sig is None:
            return None
        domain, direction = sig
        devs = ambient_domain_devices(reg, domain)
        if not devs:
            return None
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        if len(devs) > 1:
            self.executor.metrics.ambient_clarifications += 1
            self.store.set_pending(ctx.user_id, ctx.session_id, {"domain": domain, "direction": direction})
            return Routed(text=ambient_clarifying_text(domain, reg))
        call = ambient_default_call(domain, direction, devs[0])
        if call is None:
            return None
        calls = ensure_power_dependency([call], reg)
        trace.note_calls(calls)
        outs = await self.executor.run_calls(ctx, calls)
        return Routed(outs=outs, say=describe_outcomes(outs, reg))

    async def _relative(self, ctx: ReqCtx, res, trace: TurnTrace) -> Routed | None:
        """"raise the fan speed by 2": the resolver named the endpoint and capability but never reads live state
        (no I/O there), so this fetches the current value with the same `get_state` tool the executor always uses,
        computes and clamps the new value against the capability's own announced range, then runs it through the
        ordinary `set_state` path (including the generic power-dependency rule). Returns None (fall through to the
        LLM) only when the current value genuinely cannot be read or the capability has no usable range -- never a
        guess."""
        ep, cap_id, direction, amount, pct = res.relative
        trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
        out_get = await self.executor.run_call(ctx, "get_state", {"endpoint_id": ep})
        if not out_get.ok:
            return Routed(outs=[out_get], say="")
        states = out_get.result.get("states") or []
        cur = states[0].get("values", {}).get(cap_id) if states else None
        d = self.registry.devices.get(ep)
        c = d.cap(cap_id) if d else None
        if not isinstance(cur, (int, float)) or isinstance(cur, bool) or c is None or c.min is None or c.max is None:
            return None
        if amount is None:
            step = max(1, round((c.max - c.min) * 0.1))
        else:
            step = amount * (c.max - c.min) / 100 if pct else amount
        target = max(c.min, min(c.max, cur + direction * step))
        if c.type == "int":
            target = int(round(target))
        err, clean = validate_call(self.registry, "set_state", {"endpoint_id": ep, "capability": cap_id, "value": target})
        if err:
            return None
        calls = ensure_power_dependency([{"tool": "set_state", "args": clean}], self.registry)
        outs = await self.executor.run_calls(ctx, calls)
        verb = "raised" if direction > 0 else "lowered"
        pct = "%" if c.min == 0 and c.max == 100 else ""
        shown = int(target) if c.type == "int" else target
        return Routed(outs=outs, say=f"{d.name} {verb} to {shown}{pct}.")

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
                if not self.cfg.allows_fast("status"):            # a sensor reading is a read-only question: the LLM's
                    continue
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
