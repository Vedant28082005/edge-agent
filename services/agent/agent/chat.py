"""The chat turn (POST /v1/chat): user message -> prompt -> planner -> executor -> reply, as SSE events.

v1 reacts to user messages only (the event bus delivers them here); the fast path / scheduler is S8. The synchronous
reply never waits for a moving action: it reports "moving", emits `final`, and the stream stays open (bounded) so the
hub's settle events can be pushed later as `action_settled` / `action_settle_timeout` (matched by call_id)."""
from __future__ import annotations

import asyncio
import logging
import time

from .devices import DeviceRegistry
from .events import Event
from .executor import READ_TOOLS, CallOutcome, Executor, Metrics
from .llm import LlmBusy, LlmError
from .planner import Plan, Planner
from .prompt import PromptBudgetError, PromptBuilder, render_result
from .reqctx import ReqCtx
from .store import Store
from .util import clean_label, clean_user_text

log = logging.getLogger("agent.chat")

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
    "not_sensor": "that isn't a sensor", "bad_tool_result": "the hub returned something unexpected",
}


def _name(reg: DeviceRegistry, ep: object) -> str:
    d = reg.devices.get(ep) if isinstance(ep, str) else None
    return d.name if d else "that device"


def compose_final(say: str, outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """The model's `say` is drafted before the actions run, so it is only used as-is when everything went fine.
    Otherwise the text is built from the outcomes (never claims success for something that failed)."""
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


class ChatService:
    def __init__(self, registry: DeviceRegistry, builder: PromptBuilder, planner: Planner, executor: Executor,
                 store: Store, metrics: Metrics, settle_wait_s: float = 150.0, max_queued_turns: int = 2) -> None:
        self.registry, self.builder, self.planner, self.executor = registry, builder, planner, executor
        self.store, self.metrics, self.settle_wait_s = store, metrics, settle_wait_s
        self._active = 0
        self._max_active = 1 + max_queued_turns

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
            ctx.close()

    async def turn(self, ctx: ReqCtx, t0: float) -> None:
        text = clean_user_text(ctx.text, 400)
        if not text:
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
        user, sess = ctx.user_id, ctx.session_id
        history = self.store.recent(user, sess, 4)
        summary = self.store.summary(user, sess)
        await ctx.emit("status", {"text": "thinking"})
        outs: list[CallOutcome] = []
        extra_tail: list[dict] = []
        tool_results = ""
        say = ""
        while True:
            if not ctx.budget.take_step():
                if not outs:
                    await ctx.emit("error", {"code": "budget_exceeded", "message": "request budget exceeded"})
                    return
                say = "I stopped here because I reached this request's limit."
                break
            try:
                prompt = self.builder.build(text, history, summary, [], extra_tail, tool_results)
            except PromptBudgetError:
                log.exception("prompt over budget")
                await ctx.emit("error", {"code": "bad_request", "message": "request too large"})
                return
            try:
                plan: Plan = await self.planner.plan(prompt.messages)
            except LlmBusy:
                await ctx.emit("error", {"code": "llm_busy", "message": "the assistant is busy, try again shortly"})
                return
            except LlmError as e:
                log.warning("llm error: %s", e)
                await ctx.emit("error", {"code": "llm_busy", "message": "the language model is unavailable"})
                return
            say = plan.say
            if not plan.calls:
                break
            await ctx.emit("status", {"text": "working on it"})
            new = await self.executor.run_calls(ctx, plan.calls)
            outs += new
            reads = [o for o in new if o.tool in READ_TOOLS and o.ok]
            if not reads:
                break
            extra_tail = [{"role": "assistant", "content": plan.as_assistant_json()}]
            tool_results = "\n".join(render_result(o.tool, o.args, o.result, self.registry) for o in new if o.ok)
        final = compose_final(say, outs, self.registry)
        words = final.split(" ")
        for i in range(0, len(words), 3):
            await ctx.emit("token", {"text": " ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "")})
        self.store.add_message(user, sess, "user", text)
        self.store.add_message(user, sess, "assistant", final)
        self.store.roll_summary(user, sess)
        await ctx.emit("final", {"text": final, "route": "llm", "latency_ms": int((time.monotonic() - t0) * 1000)})
        if ctx.pending_settles:
            await ctx.wait_settles(self.settle_wait_s)
