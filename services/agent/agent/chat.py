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
from .render import ambient_clarifying_text, describe_outcomes, describe_reads
from .reply import (CAPABILITY_TEXT, is_conversation_turn, malformed_say, phrase_verdict, read_facts, read_messages,
                    say_verdict)
from .render import name as _name
from .reqctx import ReqCtx, TurnCancelled
from .schema import (ambient_default_call, ambient_domain_devices, ambient_request_signal, ensure_power_dependency,
                     is_question_intent, is_short_confirmation, plan_domain_direction, repair_plan_calls,
                     strip_writes_for_question)
from .sched.scheduler import Routed, Scheduler
from .sched.trace import TurnTrace
from .store import Store
from .views import ScopedViews
from .util import clean_user_text

log = logging.getLogger("agent.chat")

DEFAULT_IMAGE_QUESTION = "What do you see?"      # image-only turn, no text: a general describe-the-scene question
KEEPALIVE_S = 8.0      # (S8g) well under any sane SSE/proxy idle timeout, for a 15-65s [est/measured] LLM turn

NO_PLAN_TEXT = "I couldn't work out what to do."


def _read_sig(c: dict) -> tuple:
    return (c.get("tool"), tuple(sorted((c.get("args") or {}).items())))


def _names_unknown(calls: list[dict], reg: DeviceRegistry) -> bool:
    """True when a decoded plan names an endpoint, room or scene the current registry does not have."""
    for c in calls:
        a = c.get("args") if isinstance(c, dict) else None
        if not isinstance(a, dict):
            continue
        if "endpoint_id" in a and a["endpoint_id"] not in reg.devices:
            return True
        if "room" in a and a["room"] not in reg.rooms():
            return True
        if c.get("tool") == "run_scene" and reg.find_scene(a.get("scene")) is None:
            return True
    return False


def no_plan_text(reg: DeviceRegistry) -> str:
    """(S8g) The model made no tool call and gave no text either: never silently say "OK", and never invent a
    device -- name one straight from the live registry (any real one; it is only an example of the phrasing)."""
    name = next((d.name for d in reg.devices.values()), None)
    return f"{NO_PLAN_TEXT} Try naming the device, for example: turn on {name}." if name else NO_PLAN_TEXT


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
    parts.extend(tail_notes(outs, reg))
    return " ".join(parts)


def tail_notes(outs: list[CallOutcome], reg: DeviceRegistry) -> list[str]:
    """What the user must hear whoever wrote the main sentence: a still-moving or unverified action."""
    notes: list[str] = []
    for o in outs:
        if o.ok and o.state == "moving":
            notes.append(f"{_name(reg, o.args.get('endpoint_id'))} is still moving; I'll tell you when it settles.")
        elif o.ok and o.tool == "set_state" and o.verified is False:
            notes.append(f"I couldn't confirm that {_name(reg, o.args.get('endpoint_id'))} changed.")
    return notes


def apply_hb_failures(final: str, hb_failures: list[str], outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """Fold homebench's `plan.failures` (error_input / unparsed-part notes) into the turn's reply.

    - Nothing to fold, or an action already ran this turn (fast path, the ambient fallback, or the homebench
      calls themselves) -- that result IS the reply; a parse failure elsewhere in the same reply adds no error
      line on top of it.
    - A bare error_input (one or more, nothing else wrong and nothing else ran): one clear reply, never
      "Couldn't do this part: could not do this part." stacked onto the generic "I couldn't work out" text --
      that repeats the same non-information twice.
    - Any other failure (unknown method, invalid value, ...) carries real diagnostic text, so it is still
      appended when nothing else ran."""
    if not hb_failures or outs:
        return final
    if set(hb_failures) == {"could not do this part"}:
        return no_plan_text(reg)
    return final + " " + " ".join(f"Couldn't do this part: {f}." for f in hb_failures)


class ChatService:
    def __init__(self, registry: DeviceRegistry, builder: PromptBuilder, planner: Planner, executor: Executor,
                 store: Store, metrics: Metrics, settle_wait_s: float = 150.0, max_queued_turns: int = 2,
                 sched: Scheduler | None = None, views: ScopedViews | None = None, hb_table_provider=None) -> None:
        self.registry, self.builder, self.planner, self.executor = registry, builder, planner, executor
        self.views = views or ScopedViews(registry, builder)
        self.sched = sched
        self.hb_table_provider = hb_table_provider     # homebench only: Scope -> homebench.HomebenchTable
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
            return await ctx.await_cancellable(self.planner.plan(messages, scope))
        finally:
            pinger.cancel()
            try:
                await pinger
            except asyncio.CancelledError:
                pass

    def _vision_enabled(self) -> bool:
        """A node is enabled when the router is on and at least one vision/yolo backend is registered."""
        return bool(self.sched and self.sched.cfg.router and self.sched.router.avail.backends)

    async def _ambient_empty_plan(self, ctx: ReqCtx, text: str, reg: DeviceRegistry, outs: list[CallOutcome],
                                  trace: TurnTrace | None) -> str | None:
        """(S8j) The scheduler's own ambient fast path (sched/scheduler.py `_ambient`) normally handles an ambient
        complaint before the LLM ever runs; this is the backstop for when it is disabled or did not match (e.g. an
        earlier read this turn took a different fallthrough): a model that gave up and returned an empty plan for
        "it's dark/bright/..." is answered with the ambient rule -- act when exactly one device can answer and has
        the needed capability, ask when more than one could -- never the generic "I couldn't work out" text. None
        when the text names no ambient complaint at all, so the caller falls back to its own default text."""
        sig = ambient_request_signal(text, reg)
        if sig is None:
            return None
        domain, direction = sig
        devs = ambient_domain_devices(reg, domain)
        if not devs:
            return None
        if len(devs) > 1:
            self.metrics.ambient_clarifications += 1
            self.store.set_pending(ctx.user_id, ctx.session_id, {"domain": domain, "direction": direction})
            return ambient_clarifying_text(domain, reg)
        call = ambient_default_call(domain, direction, devs[0])
        if call is None:
            return None
        new = await self.executor.run_calls(ctx, [call])
        outs += new
        if trace is not None:
            trace.note_calls([call])
            trace.note_executed(new)
        return " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or None

    @staticmethod
    def _tail(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
        notes = tail_notes(outs, reg)
        return (" " + " ".join(notes)) if notes else ""

    def _choose_reply(self, agent_final: str, model_say: str, outs: list[CallOutcome], reg: DeviceRegistry, user_text: str,
                      stripped: bool, trace: TurnTrace | None) -> tuple[str, str]:
        """(S14) (text, "model" | "agent"). The agent's verified text stands unless the model's `say` passes
        `reply.say_verdict`; the still-moving / unverified notes are always kept."""
        if not model_say:
            return agent_final, "agent"
        if self.sched is not None and self.sched.cfg.allow_say:
            verdict = say_verdict(model_say, outs, reg, describe_outcomes(outs, reg), user_text, stripped)
        else:                                  # say_in_plan off: the text is never shown, but junk still must not leak
            m = malformed_say(model_say, reg)
            verdict = ("malformed_" + m) if m else "disabled"
        if trace is not None:
            trace.say_verdict = verdict or "ok"
        if verdict is not None:
            # a no-call turn whose say was refused gets the capability line, never the bare "couldn't work out" text
            if not outs and verdict != "disabled" and agent_final.startswith(NO_PLAN_TEXT):
                return CAPABILITY_TEXT, "agent"
            return agent_final, "agent"
        return model_say + self._tail(outs, reg), "model"

    async def _natural_read(self, ctx: ReqCtx, text: str, outs: list[CallOutcome], reg: DeviceRegistry,
                            trace: TurnTrace | None, hb_failures: list[str]) -> str | None:
        """(S14, flag natural_read_replies) A second, short LLM call that phrases a read-only answer from the read results,
        streamed as `token` events. None (keep the agent's own text) when the flag is off, the turn did anything but
        succeed at reads, the request budget is spent, the model is busy, or the phrasing contradicts the results."""
        if (self.sched is None or not self.sched.cfg.natural_read_replies or hb_failures or not outs
                or not all(o.ok and o.is_read for o in outs)):
            return None
        facts = read_facts(outs, reg)
        if not facts or not ctx.budget.take_step():
            return None
        cfg = self.sched.cfg
        llm, msgs, parts = self.planner.llm, read_messages(text, facts), []
        t0 = time.monotonic()

        async def run() -> None:
            if hasattr(llm, "stream_text"):
                async for chunk in llm.stream_text(msgs, cfg.read_reply_max_tokens):
                    parts.append(chunk)
                    await ctx.emit("token", {"text": chunk})
            else:
                parts.append((await llm.complete(msgs, None, max_tokens=cfg.read_reply_max_tokens)).content)

        try:
            await ctx.await_cancellable(run())
        except (LlmBusy, LlmError) as e:
            log.warning("natural read reply failed: %s", e)
            return None
        phrased = " ".join("".join(parts).split())
        if trace is not None:
            trace.llm_calls += 1
            trace.add_stage("phrase_ms", (time.monotonic() - t0) * 1000)
        verdict = phrase_verdict(phrased, facts)
        if trace is not None:
            trace.say_verdict = "phrase_" + (verdict or "ok")
        return phrased if verdict is None else None

    async def _resolve_pending_confirmation(self, ctx: ReqCtx, text: str, reg: DeviceRegistry,
                                            trace: TurnTrace | None) -> "Routed | None":
        """A bare "yes"-shaped reply to the clarifying question this session was just asked (ambient domain +
        direction, stashed by whichever side asked it: the scheduler's fast path or this service's own LLM-loop
        polarity check below) resolves using THAT original intent, never the confirmation text itself (which names
        no domain/direction/device). Discards the pending entry on any reply, confirmation or not -- a changed
        subject abandons the question rather than leaving it to misfire on some later, unrelated "yes"."""
        pending = self.store.pop_pending(ctx.user_id, ctx.session_id)
        if pending and pending.get("kind") == "disambig":
            # (S15 A3) the answer to "which one do you mean?": re-run the original request on exactly the device(s) named
            return await self.sched.answer_which(ctx, text, pending, trace) if self.sched else None
        if not pending or not is_short_confirmation(text):
            return None
        devs = ambient_domain_devices(reg, pending["domain"])
        if len(devs) != 1:
            return None            # still ambiguous: a bare "yes" cannot pick which device, so this does nothing
        call = ambient_default_call(pending["domain"], pending["direction"], devs[0])
        if call is None:
            return None
        calls = ensure_power_dependency([call], reg)
        if trace is not None:
            trace.route, trace.sse_route, trace.source = "fast", "fast", "fast_path"
            trace.note_calls(calls)
        outs = await self.executor.run_calls(ctx, calls)
        if trace is not None:
            trace.note_executed(outs)
        return Routed(outs=outs, say=describe_outcomes(outs, reg))

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
        except TurnCancelled:
            # (S8o) the browser aborted the stream: nothing is answered or remembered; what already ran stays
            log.info("turn %s cancelled", ctx.req_id)
            if trace is not None:
                trace.outcome = "cancelled"
        finally:
            if self.sched and trace is not None:
                self.sched.emit(ctx, trace)

    async def _turn_traced(self, ctx: ReqCtx, text: str, t0: float, trace: TurnTrace | None) -> None:
        user, sess, guest = ctx.user_id, ctx.session_id, ctx.role == "guest"
        view = self.views.get(ctx.scope)               # what THIS caller may see: names outside the scope never reach the model
        reg = view.registry
        route_t0 = time.monotonic()
        routed = await self._resolve_pending_confirmation(ctx, text, reg, trace)
        if routed is None:
            routed = await self.sched.route(ctx, text, trace) if self.sched else None
        if trace is not None:
            trace.add_stage("route_ms", (time.monotonic() - route_t0) * 1000)
            if routed is not None and ctx.cancelled:
                trace.note_executed(routed.outs)
        ctx.check_cancelled()
        outs: list[CallOutcome] = []
        route = "llm"
        final: str | None = None
        text_source, streamed = "agent", False
        if routed is not None:
            outs, route = routed.outs, routed.sse_route
            if trace is not None:
                trace.note_executed(outs)
            await ctx.emit("status", {"text": "working on it"})
            final = routed.text if routed.text is not None else compose_final(routed.say, outs, reg)
            if routed.note:
                final = f"{final} {routed.note}"
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
            retried_stale = False
            model_say = ""                      # (S14) the model's optional text from its last round; see reply.say_verdict
            stripped_any = False
            hb_table = self.hb_table_provider(ctx.scope) if (self.hb_table_provider
                                                             and view.builder.format == "homebench") else None
            # (homebench) the fine-tuned model was never trained on state questions -- those are answered by the
            # fast path / read tools upstream (`sched.route`), never by calling the LLM here.
            homebench_question = view.builder.format == "homebench" and is_question_intent(text)
            hb_failures: list[str] = []
            while not homebench_question:
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
                    self.sched.note_llm(trace, prompt.prefix_hash, plan.timings, bool(self.planner.llm.constrained), plan.raw,
                                        prompt.roster_hash)
                if prompt.roster_hash and plan.calls:
                    # (S15 B3) the device list changed while the model was working: a plan that names a device or scene that no
                    # longer exists is rejected (never executed) and re-planned once against the rebuilt roster.
                    fresh = self.views.get(ctx.scope)
                    if fresh.builder.roster_hash != prompt.roster_hash and _names_unknown(plan.calls, fresh.registry):
                        self.metrics.stale_plans_rejected += 1
                        if trace is not None:
                            trace.fallbacks.append("stale_roster")
                        if not retried_stale:
                            retried_stale, view, reg = True, fresh, fresh.registry
                            extra_tail, tool_results = [], ""
                            continue
                        say = "The device list just changed, so I did nothing. Please ask again."
                        break
                model_say = plan.say
                if plan.failures:        # homebench only: error_input / unparsed parts, reported alongside whatever ran
                    hb_failures.extend(plan.failures)
                conversation = False
                if plan.calls and not outs and is_conversation_turn(text, reg, plan.calls):
                    plan.calls, conversation = [], True        # "hello" / "thanks": the model's habitual read is dropped
                if conversation:
                    say = CAPABILITY_TEXT
                    break
                if not plan.calls:
                    # the model has no text channel (S8i): either this is "I'm done" after a read earlier this turn
                    # (narrate it), an ambient complaint the model gave up on (act/ask on it directly rather than
                    # the generic fallback, S8j), or there was never anything to act on at all (device-naming fallback).
                    ambient_acted = await self._ambient_empty_plan(ctx, text, reg, outs, trace)
                    if ambient_acted is not None:
                        say = ambient_acted
                    else:
                        say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or no_plan_text(reg)
                    break
                plan.calls = repair_plan_calls(plan.calls, text, reg)
                if is_question_intent(text):
                    # Bug (S8j): a question/status request must never cause a write, whatever the model proposed.
                    plan.calls, stripped = strip_writes_for_question(plan.calls)
                    if stripped:
                        stripped_any = True
                        self.metrics.writes_stripped_for_question += len(stripped)
                        if trace is not None:
                            trace.note_stripped_writes(stripped)
                        log.warning("stripped %d write call(s) from a question turn", len(stripped))
                    if not plan.calls:
                        say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or no_plan_text(reg)
                        break
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
                            extra_tail = [{"role": "assistant", "content": plan.as_assistant_json(aliases, hb_table)}]
                            tool_results = ("Contradiction: this plan moves the opposite way from what the user's "
                                            "wording asked for. Reconsider the direction, or ask instead.")
                            continue
                        self.metrics.ambient_clarifications += 1
                        self.store.set_pending(user, sess, {"domain": domain, "direction": expected})
                        say = ambient_clarifying_text(domain, reg)
                        break
                # (S8l) loop guard: never re-run a get_state/read_sensor call this turn already has a verified
                # result for -- a buggy (or merely repetitive) model's plan must not re-read the same endpoint
                # over and over until the request budget runs out. Also dedupes within this very plan (two
                # identical read calls in the same round), not just across rounds.
                seen_reads = {_read_sig({"tool": o.tool, "args": o.args}) for o in outs if o.ok and o.tool in READ_TOOLS}
                deduped: list[dict] = []
                for c in plan.calls:
                    if c.get("tool") in READ_TOOLS:
                        rsig = _read_sig(c)
                        if rsig in seen_reads:
                            continue
                        seen_reads.add(rsig)
                    deduped.append(c)
                if len(deduped) != len(plan.calls):
                    log.warning("dropped %d duplicate read call(s) already answered this turn", len(plan.calls) - len(deduped))
                    plan.calls = deduped
                    if not plan.calls:
                        say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or say or no_plan_text(reg)
                        break
                if trace is not None:
                    trace.note_calls(plan.calls)
                await ctx.emit("status", {"text": "working on it"})
                exec_t0 = time.monotonic()
                new = await self.executor.run_calls(ctx, plan.calls)
                if trace is not None:
                    trace.add_stage("exec_ms", (time.monotonic() - exec_t0) * 1000)
                    trace.note_executed(new)
                ctx.check_cancelled()              # (S8o) executor skipped what was left; do not go back to the LLM
                outs += new                        # kept even on a retry, so a model that fixes nothing still reports
                                                     # the real rejection rather than whatever it says on the retry
                rejected = [o for o in new if o.state == "rejected"]
                if new and len(rejected) == len(new) and not retried_validation:
                    # (S8g) the plan failed validation (unknown endpoint/capability, bad type, out of range, ...):
                    # give the model one retry with the reason before falling back, instead of reporting a failure
                    # for a mistake the model can usually fix itself on the next round.
                    retried_validation = True
                    extra_tail = [{"role": "assistant", "content": plan.as_assistant_json(aliases, hb_table)}]
                    tool_results = "\n".join(f"{o.tool} rejected: {o.note or 'invalid'}" for o in rejected)
                    continue
                reads = [o for o in new if o.tool in READ_TOOLS and o.ok]
                if not reads:
                    # a pure action turn (or an action after an earlier read this same turn): the confirmation is
                    # built from the verified results, never the model's (it has none to draft, S8i).
                    say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or say
                    break
                if is_question_intent(text):
                    # (S8l) loop guard: a question/status turn stops here, as soon as it has a verified read --
                    # it never goes back to the LLM to ask whether it's done (the model has no text channel to
                    # answer that with anyway, S8i). This is the other half of the bug fix above: it is what
                    # stops a model that keeps calling get_state instead of stopping, before it ever gets the
                    # chance to repeat itself into the request budget limit.
                    say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or say
                    break
                extra_tail = [{"role": "assistant", "content": plan.as_assistant_json(aliases, hb_table)}]
                tool_results = "\n".join(render_result(o.tool, o.args, o.result, reg, aliases) for o in new if o.ok)
            if homebench_question:
                say = " ".join(p for p in (describe_outcomes(outs, reg), describe_reads(outs, reg)) if p) or no_plan_text(reg)
            if self.sched is not None and self.sched.cfg.deterministic_fixes and not hb_failures:
                say = await self.sched.filter_reads_reply(ctx, text, outs, reg, say)       # (S15 A4) "which are on" lists only those
            final = apply_hb_failures(compose_final(say, outs, reg), hb_failures, outs, reg)
            final, text_source = self._choose_reply(final, model_say, outs, reg, text, stripped_any, trace)
            phrased = await self._natural_read(ctx, text, outs, reg, trace, hb_failures)
            if phrased is not None:
                final, text_source, streamed = phrased + self._tail(outs, reg), "model", True
        if ctx.images and route not in ("vlm", "yolo"):        # those routes already answered using the image
            final += " (I can't analyse images yet.)"
        if not streamed:                       # a streamed phrasing already put its words on the wire
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
            trace.text_source = text_source
            if trace.outcome == "ok":
                trace.outcome = outcome_of(outs)
            if routed is None:
                trace.route, trace.sse_route, trace.source = "llm", "llm", "llm"
        await ctx.emit("final", {"text": final, "route": route, "latency_ms": int((time.monotonic() - t0) * 1000)})
        if self.sched and trace is not None:
            self.sched.emit(ctx, trace)                  # before the settle wait, so latency_ms is the synchronous reply
        if ctx.pending_settles:
            await ctx.wait_settles(self.settle_wait_s)
