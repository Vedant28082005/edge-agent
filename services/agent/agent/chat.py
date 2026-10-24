"""The chat turn (POST /v1/chat): user message -> prompt -> planner -> executor -> reply, as SSE events.

v1 reacts to user messages only (the event bus delivers them here); the fast path / scheduler is S8. The synchronous
reply never waits for a moving action: it reports "moving", emits `final`, and the stream stays open (bounded) so the
hub's settle events can be pushed later as `action_settled` / `action_settle_timeout` (matched by call_id)."""
from __future__ import annotations

import asyncio
import logging
import time

from .aliases import AliasMap
from .devices import DeviceRegistry
from .events import Event
from .executor import READ_TOOLS, CallOutcome, Executor, Metrics
from .llm import LlmBusy, LlmError
from .planner import Plan, Planner
from .prompt import PromptBudgetError, PromptBuilder, render_result
from .reqctx import ReqCtx
from .schema import ambient_domain_devices, ambient_request_signal, plan_domain_direction, repair_plan_calls
from .sched.scheduler import Scheduler
from .sched.trace import TurnTrace
from .store import Store
from .views import ScopedViews
from .util import clean_label, clean_user_text

log = logging.getLogger("agent.chat")

DEFAULT_IMAGE_QUESTION = "What do you see?"      # image-only turn, no text: a general describe-the-scene question
KEEPALIVE_S = 8.0      # (S8g) well under any sane SSE/proxy idle timeout, for a 15-65s [est/measured] LLM turn

NO_PLAN_TEXT = "I couldn't work out what to do."


def no_plan_text(reg: DeviceRegistry) -> str:
    """(S8g) The model made no tool call and gave no text either: never silently say "OK", and never invent a
    device -- name one straight from the live registry (any real one; it is only an example of the phrasing)."""
    name = next((d.name for d in reg.devices.values()), None)
    return f"{NO_PLAN_TEXT} Try naming the device, for example: turn on {name}." if name else NO_PLAN_TEXT


# S8h: an ambient complaint ("it's dark/bright/hot/cold/stuffy in here") names no device and no number. Acting is
# only safe when exactly one device answers that domain and the plan's own direction is unambiguous; otherwise ask,
# naming the real devices from the registry rather than guessing one.
def ambient_clarifying_text(domain: str, reg: DeviceRegistry) -> str:
    names = [d.name for d in ambient_domain_devices(reg, domain)]
    if not names:
        return "Which device do you mean?"
    if len(names) == 1:
        return f"Did you mean {names[0]}?"
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return f"Which one do you mean — {shown}?"


ERROR_TEXT = {
    "unknown_endpoint": "that device doesn't exist", "unknown_capability": "that device can't do that",
    "not_actuator": "that can't be controlled", "bad_type": "that value isn't valid for the device",
    "out_of_range": "that value is out of range", "unknown_room": "I don't know that room",
    "unknown_tool": "I can't do that", "bad_args": "I couldn't work out that request",
    "unknown_scene": "I don't know that scene", "scene_broken": "that scene is broken and needs repair",
    "offline": "is offline", "disabled": "is disabled", "ack_timeout": "did not respond",
    "device_error": "reported an error", "hub_unavailable": "the device hub is unreachable",
    "confirmation_required": "needs your confirmation", "budget_exceeded": "I ran out of budget for this request",
    "not_allowed": "guests can't do that", "blocked_notify_only": "needs your confirmation in the app",
    "out_of_scope": "that isn't available to you", "not_sensor": "that isn't a sensor", "bad_tool_result": "the hub returned something unexpected",
}


def _name(reg: DeviceRegistry, ep: object) -> str:
    d = reg.devices.get(ep) if isinstance(ep, str) else None
    return d.name if d else "that device"


def outcome_of(outs: list) -> str:
    if not outs:
        return "ok"
    bad = [o for o in outs if not o.ok]
    if not bad:
        return "ok"
    if any(o.state in ("declined", "expired", "blocked", "denied") for o in bad):
        return "needs_confirmation" if any(o.state in ("declined", "expired", "blocked") for o in bad) else "denied"
    return "partial" if len(bad) < len(outs) else "failed"


def compose_final(say: str, outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """`say` is always agent-composed by now (`describe_outcomes` / `describe_reads` / `no_plan_text`: the model has
    no text channel at all, S8i), so it is only used as-is when everything went fine; otherwise the text is built
    from the outcomes (never claims success for something that failed)."""
    acts = [o for o in outs if not o.is_read or not o.ok]
    bad = [o for o in acts if not o.ok]
    parts: list[str] = []
    if bad:
        for o in outs:
            if o.ok:
                continue
            n = _name(reg, o.args.get("endpoint_id")) if "endpoint_id" in o.args else None
            if o.state == "declined":
                parts.append("OK, I won't do that.")
            elif o.state == "expired":
                parts.append("The confirmation expired, so nothing was done.")
            else:
                err = str(o.result.get("error") or o.note or "failed")
                msg = ERROR_TEXT.get(err, "something went wrong")
                if err in ("offline", "disabled", "ack_timeout", "device_error") and n:
                    parts.append(f"{n} {msg}.")
                else:
                    parts.append(f"I couldn't do that: {msg}.")
        done = [o for o in outs if o.ok and o.tool in ("set_state", "run_scene")]
        if done:
            parts.insert(0, "Some actions went through.")
    else:
        parts.append(say.strip() or ("Done." if any(o.tool in ("set_state", "run_scene") for o in outs) else "OK."))
    for o in outs:
        if o.ok and o.state == "moving":
            parts.append(f"{_name(reg, o.args.get('endpoint_id'))} is still moving; I'll tell you when it settles.")
        elif o.ok and o.tool == "set_state" and o.verified is False:
            parts.append(f"I couldn't confirm that {_name(reg, o.args.get('endpoint_id'))} changed.")
    return " ".join(parts)


def describe_outcomes(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """The confirmation text for a pure action turn, built by the agent from the verified outcomes (S8f): the model
    is not trusted to draft it, so its `say` is never used here. Empty when nothing to act on was actually run."""
    acted = [o for o in outs if o.ok and o.tool in ("set_state", "run_scene")]
    if not acted:
        return ""
    on_eps = {o.args.get("endpoint_id") for o in acted if o.tool == "set_state" and o.args.get("capability") == "power"
              and o.args.get("value") is True}
    # (S8g) generic: any other set_state on the same endpoint in this turn is narrated together with the power-on,
    # not as a separate "turned on." sentence -- whatever capability it is, not just brightness.
    other_cap_eps = {o.args.get("endpoint_id") for o in acted if o.tool == "set_state" and o.args.get("capability") != "power"}
    parts: list[str] = []
    for o in acted:
        if o.tool == "run_scene":
            parts.append(f"Running {o.args.get('scene')}.")
            continue
        cap, val, ep = o.args.get("capability"), o.args.get("value"), o.args.get("endpoint_id")
        name = _name(reg, ep)
        if cap == "power":
            if val is True and ep in other_cap_eps:
                continue                          # narrated together with the dependent call below
            parts.append(f"{name} turned {'on' if val else 'off'}.")
        elif cap == "lock":
            parts.append(f"{name} {'unlocked' if val == 'unlocked' else 'locked'}.")
        elif cap == "position":
            parts.append(f"{name} {'opened' if val else 'closed'}.")
        elif cap == "brightness":
            if ep in on_eps:
                parts.append(f"{name} turned on at {val}%.")
            else:
                parts.append(f"{name} brightness set to {val}.")
        elif ep in on_eps:
            parts.append(f"{name} turned on; {cap} set to {val}.")
        else:
            parts.append(f"{name} {cap} set to {val}.")
    return " ".join(parts)


def _fmt_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return clean_label(v, 24) or "?"


def describe_reads(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """Text for a pure-read turn (read_sensor / get_state), built by the agent from the verified results, the same
    reasoning as `describe_outcomes` extended to reads: the model has no text channel to report a reading with
    (S8i), so this is the only place a `read_sensor`/`get_state` value ever reaches the user. Values come straight
    from the hub's own result fields, never a model draft, and are re-sanitised (`clean_label`) like every other
    device-derived string."""
    reads = [o for o in outs if o.ok and o.tool in ("read_sensor", "get_state")]
    if not reads:
        return ""
    parts: list[str] = []
    for o in reads:
        if o.tool == "read_sensor":
            name = _name(reg, o.args.get("endpoint_id"))
            cap = clean_label(o.result.get("capability"), 24)
            unit = clean_label(o.result.get("unit"), 8)
            stale = " (last known)" if o.result.get("stale") else ""
            parts.append(f"{name} {cap}: {_fmt_value(o.result.get('value'))}{unit}{stale}.")
        else:
            for s in o.result.get("states") or []:
                if not isinstance(s, dict):
                    continue
                name = _name(reg, s.get("endpoint_id"))
                if not s.get("online"):
                    parts.append(f"{name}: offline.")
                    continue
                vs = ", ".join(f"{clean_label(k, 24)}={_fmt_value(v)}" for k, v in sorted((s.get("values") or {}).items()))
                parts.append(f"{name}: {vs}." if vs else f"{name}: no data.")
    return " ".join(parts)


class ChatService:
    def __init__(self, registry: DeviceRegistry, builder: PromptBuilder, planner: Planner, executor: Executor,
                 store: Store, metrics: Metrics, settle_wait_s: float = 150.0, max_queued_turns: int = 2,
                 sched: Scheduler | None = None, views: ScopedViews | None = None) -> None:
        self.registry, self.builder, self.planner, self.executor = registry, builder, planner, executor
        self.views = views or ScopedViews(registry, builder)
        self.sched = sched
        self.store, self.metrics, self.settle_wait_s = store, metrics, settle_wait_s
        self._active = 0
        self._max_active = 1 + max_queued_turns

    def busy(self) -> bool:
        return self._active >= self._max_active

    async def _plan_with_keepalive(self, ctx: ReqCtx, messages: list[dict], scope) -> Plan:
        """(S8g) One real LLM round takes 15-65 s [est/measured]; without traffic on the SSE stream for that whole
        time, a proxy or the gateway's own idle/read timeout can give up on the stream well before `final` arrives,
        so the browser sees "No reply" even though the turn (and any actions in it) completed normally on the agent.
        A periodic `status` event (already a defined, display-only SSE event) keeps the stream alive while the
        model generates; it is purely cosmetic and never substitutes for the real `final`."""
        async def ping() -> None:
            while True:
                await asyncio.sleep(KEEPALIVE_S)
                await ctx.emit("status", {"text": "still working on it"})

        pinger = asyncio.get_running_loop().create_task(ping())
        try:
            return await self.planner.plan(messages, scope)
        finally:
            pinger.cancel()
            try:
                await pinger
            except asyncio.CancelledError:
                pass

    def _vision_enabled(self) -> bool:
        """A node is enabled when the router is on and at least one vision/yolo backend is registered."""
        return bool(self.sched and self.sched.cfg.router and self.sched.router.avail.backends)

    async def on_user_message(self, event: Event) -> None:
        ctx: ReqCtx = event.data["ctx"]
        t0 = time.monotonic()
        try:
            await self.turn(ctx, t0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("turn failed")
            await ctx.emit("error", {"code": "bad_request", "message": "internal error"})
        finally:
            ctx.drop_images()                                # image bytes live for this turn only
            ctx.close()

    async def turn(self, ctx: ReqCtx, t0: float) -> None:
        text = clean_user_text(ctx.text, 400)
        if not text:
            if ctx.images and self._vision_enabled():         # image-only turn: ask a general describe question via the vision route
                text = DEFAULT_IMAGE_QUESTION
            elif ctx.images:                                   # no node enabled: nothing to analyse them with
                await ctx.emit("final", {"text": "I can't analyse images yet.", "route": "llm", "latency_ms": 0})
                return
            else:
                await ctx.emit("error", {"code": "bad_request", "message": "empty message"})
                return
        if self._active >= self._max_active:
            await ctx.emit("error", {"code": "llm_busy", "message": "the assistant is busy, try again shortly"})
            return
        self._active += 1
        try:
            await self._turn(ctx, text, t0)
        finally:
            self._active -= 1

    async def _turn(self, ctx: ReqCtx, text: str, t0: float) -> None:
        trace = self.sched.new_trace(ctx) if self.sched else None
        try:
            await self._turn_traced(ctx, text, t0, trace)
        finally:
            if self.sched and trace is not None:
                self.sched.emit(ctx, trace)

    async def _turn_traced(self, ctx: ReqCtx, text: str, t0: float, trace: TurnTrace | None) -> None:
        user, sess, guest = ctx.user_id, ctx.session_id, ctx.role == "guest"
        view = self.views.get(ctx.scope)               # what THIS caller may see: names outside the scope never reach the model
        reg = view.registry
        route_t0 = time.monotonic()
        routed = await self.sched.route(ctx, text, trace) if self.sched else None
        if trace is not None:
            trace.add_stage("route_ms", (time.monotonic() - route_t0) * 1000)
        outs: list[CallOutcome] = []
        route = "llm"
        final: str | None = None
        if routed is not None:
            outs, route = routed.outs, routed.sse_route
            if trace is not None:
                trace.note_executed(outs)
            await ctx.emit("status", {"text": "working on it"})
            final = routed.text if routed.text is not None else compose_final(routed.say, outs, reg)
        else:
            if self.sched:
                win = self.sched.memory.load(user, sess, guest)
                history, summary = win.history, win.summary
            else:
                history, summary = self.store.recent(user, sess, 4, guest), self.store.summary(user, sess, guest)
            memories = [m["text"] for m in self.store.list_memories(user, guest)[:5]]
            aliases = self.sched.aliases(ctx.scope) if self.sched else AliasMap()
            await ctx.emit("status", {"text": "working on it"})   # immediately: no blank bubble while the LLM runs
            extra_tail: list[dict] = []
            tool_results = ""
            say = ""
            retried_validation = False
            retried_polarity = False
            while True:
                if not ctx.budget.take_step():
                    if not outs:
                        await ctx.emit("error", {"code": "budget_exceeded", "message": "request budget exceeded"})
                        if trace:
                            trace.outcome = "error:budget_exceeded"
                        return
                    say = "I stopped here because I reached this request's limit."
                    break
                try:
                    prompt = view.builder.build(text, history, summary, memories, extra_tail, tool_results)
                except PromptBudgetError:
                    log.exception("prompt over budget")
                    await ctx.emit("error", {"code": "bad_request", "message": "request too large"})
                    if trace:
                        trace.outcome = "error:prompt_budget"
                    return
                llm_t0 = time.monotonic()
                try:
                    plan: Plan = await self._plan_with_keepalive(ctx, prompt.messages, ctx.scope)
                except LlmBusy:
                    await ctx.emit("error", {"code": "llm_busy", "message": "the assistant is busy, try again shortly"})
                    if trace:
                        trace.outcome = "error:llm_busy"
                    return
                except LlmError as e:
                    log.warning("llm error: %s", e)
                    await ctx.emit("error", {"code": "llm_busy", "message": "the language model is unavailable"})
                    if trace:
                        trace.outcome = "error:llm_unavailable"
                    return
                if trace is not None:
                    trace.add_stage("llm_ms", (time.monotonic() - llm_t0) * 1000)
                if self.sched and trace is not None:
                    self.sched.note_llm(trace, prompt.prefix_hash, plan.timings, bool(self.planner.llm.constrained), plan.raw)
                if not plan.calls:
                    # the model has no text channel (S8i): either this is "I'm done" after a read earlier this turn
                    # (narrate it) or there was never anything to act on at all (the safe, device-naming fallback).
                    say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or no_plan_text(reg)
                    break
                plan.calls = repair_plan_calls(plan.calls, text, reg)
                sig = ambient_request_signal(text, reg)
                if sig is not None:
                    domain, expected = sig
                    devs = ambient_domain_devices(reg, domain)
                    actual = plan_domain_direction(domain, plan.calls) if len(devs) == 1 else None
                    if len(devs) != 1 or actual != expected:
                        if len(devs) == 1 and not retried_polarity:
                            # one candidate device but the plan moved the wrong way: give the model one retry
                            # with the contradiction stated before falling back to a clarifying question.
                            retried_polarity = True
                            extra_tail = [{"role": "assistant", "content": plan.as_assistant_json(aliases)}]
                            tool_results = ("Contradiction: this plan moves the opposite way from what the user's "
                                            "wording asked for. Reconsider the direction, or ask instead.")
                            continue
                        self.metrics.ambient_clarifications += 1
                        say = ambient_clarifying_text(domain, reg)
                        break
                if trace is not None:
                    trace.note_calls(plan.calls)
                await ctx.emit("status", {"text": "working on it"})
                exec_t0 = time.monotonic()
                new = await self.executor.run_calls(ctx, plan.calls)
                if trace is not None:
                    trace.add_stage("exec_ms", (time.monotonic() - exec_t0) * 1000)
                    trace.note_executed(new)
                outs += new                        # kept even on a retry, so a model that fixes nothing still reports
                                                     # the real rejection rather than whatever it says on the retry
                rejected = [o for o in new if o.state == "rejected"]
                if new and len(rejected) == len(new) and not retried_validation:
                    # (S8g) the plan failed validation (unknown endpoint/capability, bad type, out of range, ...):
                    # give the model one retry with the reason before falling back, instead of reporting a failure
                    # for a mistake the model can usually fix itself on the next round.
                    retried_validation = True
                    extra_tail = [{"role": "assistant", "content": plan.as_assistant_json(aliases)}]
                    tool_results = "\n".join(f"{o.tool} rejected: {o.note or 'invalid'}" for o in rejected)
                    continue
                reads = [o for o in new if o.tool in READ_TOOLS and o.ok]
                if not reads:
                    # a pure action turn (or an action after an earlier read this same turn): the confirmation is
                    # built from the verified results, never the model's (it has none to draft, S8i).
                    say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or say
                    break
                extra_tail = [{"role": "assistant", "content": plan.as_assistant_json(aliases)}]
                tool_results = "\n".join(render_result(o.tool, o.args, o.result, reg, aliases) for o in new if o.ok)
            final = compose_final(say, outs, reg)
        if ctx.images and route not in ("vlm", "yolo"):        # those routes already answered using the image
            final += " (I can't analyse images yet.)"
        words = final.split(" ")
        for i in range(0, len(words), 3):
            await ctx.emit("token", {"text": " ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "")})
        self.store.add_message(user, sess, "user", text, guest)
        self.store.add_message(user, sess, "assistant", final, guest)
        if self.sched:
            self.sched.memory.fold(user, sess, guest=guest)
        else:
            self.store.roll_summary(user, sess, guest=guest)
        if trace is not None:
            if trace.outcome == "ok":
                trace.outcome = outcome_of(outs)
            if routed is None:
                trace.route, trace.sse_route, trace.source = "llm", "llm", "llm"
        await ctx.emit("final", {"text": final, "route": route, "latency_ms": int((time.monotonic() - t0) * 1000)})
        if self.sched and trace is not None:
            self.sched.emit(ctx, trace)                  # before the settle wait, so latency_ms is the synchronous reply
        if ctx.pending_settles:
            await ctx.wait_settles(self.settle_wait_s)
