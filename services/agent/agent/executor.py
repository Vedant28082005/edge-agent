"""Executor, separate from the planner. The planner only proposes a list of tool calls; this module validates each one
against the live registry, applies the confirmation policy, runs it through the hub's MCP endpoint under the request's
budgets, and records an action-log entry (trigger, reason, action, result).

Out-of-band fields (confirmation_token, _meta.user_id, _meta.call_id) are attached HERE and nowhere else; they are not
in any schema the model sees, a copy in the model's own arguments is stripped and logged (names only), and the token
never reaches events, logs or the action log. Verification: the hub's `set_state` result carries the read-back
(`status: done|moving`, `verified`); motion is verified asynchronously via the settle events (see settle.py). Nothing
here ever blocks on a moving action."""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from .confirmations import ConfirmationManager
from .devices import DeviceRegistry
from .hub_client import HubUnavailable, McpClient
from .notify import Notifier
from .policy import Action, Decision, Trigger, needs_confirmation
from .reqctx import ReqCtx
from .schema import strip_forbidden, validate_call
from .settle import SettleTable, _scalar
from .store import Store
from .util import action_hash, clean_label

log = logging.getLogger("agent.executor")

READ_TOOLS = ("list_devices", "get_state", "read_sensor")
RETRYABLE = ("ack_timeout", "hub_unavailable")


@dataclass
class Metrics:
    """Counters for the S10 ablation (constrained vs unconstrained)."""
    plans: int = 0
    plan_parse_failures: int = 0
    calls_proposed: int = 0
    calls_rejected: dict = field(default_factory=dict)       # error code -> count (invalid calls caught by validation)
    calls_executed: int = 0
    stripped_fields: int = 0

    def reject(self, code: str) -> None:
        self.calls_rejected[code] = self.calls_rejected.get(code, 0) + 1

    def as_dict(self) -> dict:
        return {"plans": self.plans, "plan_parse_failures": self.plan_parse_failures,
                "calls_proposed": self.calls_proposed, "calls_rejected": dict(self.calls_rejected),
                "calls_rejected_total": sum(self.calls_rejected.values()), "calls_executed": self.calls_executed,
                "stripped_fields": self.stripped_fields}


@dataclass
class CallOutcome:
    call_id: str
    tool: str
    args: dict
    result: dict
    ok: bool
    state: str            # done | moving | rejected | failed | blocked | declined | expired | denied | budget
    verified: bool | None = None
    note: str = ""

    @property
    def is_read(self) -> bool:
        return self.tool in READ_TOOLS


class Executor:
    def __init__(self, hub: McpClient, registry: DeviceRegistry, store: Store, confirmations: ConfirmationManager,
                 settles: SettleTable, notifier: Notifier, metrics: Metrics | None = None,
                 scene_names: tuple[str, ...] = ()) -> None:
        self.hub, self.registry, self.store = hub, registry, store
        self.confirmations, self.settles, self.notifier = confirmations, settles, notifier
        self.metrics = metrics or Metrics()
        self.scene_names = scene_names

    # ------------------------------------------------------------------------------------------ public
    async def run_calls(self, ctx: ReqCtx, calls: list[dict]) -> list[CallOutcome]:
        """Run a plan. Calls on `risk: confirm` endpoints go last so one pending confirmation never delays the rest."""
        def risky(c: dict) -> bool:
            a = c.get("args") if isinstance(c, dict) else None
            d = self.registry.devices.get(a.get("endpoint_id")) if isinstance(a, dict) and isinstance(a.get("endpoint_id"), str) else None
            return bool(d and d.risk == "confirm" and c.get("tool") == "set_state")

        ordered = [c for c in calls if not risky(c)] + [c for c in calls if risky(c)]
        out: list[CallOutcome] = []
        for c in ordered:
            tool, args = (c.get("tool"), c.get("args")) if isinstance(c, dict) else (None, None)
            out.append(await self.run_call(ctx, tool, args))
        return out

    async def run_call(self, ctx: ReqCtx, tool: Any, raw_args: Any) -> CallOutcome:
        call_id = "c_" + secrets.token_hex(8)
        self.metrics.calls_proposed += 1
        if isinstance(raw_args, dict):
            _, removed = strip_forbidden(raw_args)
            if removed:
                self.metrics.stripped_fields += len(removed)
                log.warning("model supplied out-of-band argument(s) %s for %s; stripped", removed, tool)
        err, args = validate_call(self.registry, tool, raw_args, self.scene_names)
        shown_args = args if not err else (raw_args if isinstance(raw_args, dict) else {})
        shown_args = strip_forbidden(shown_args)[0] if isinstance(shown_args, dict) else {}
        tool_s = tool if isinstance(tool, str) and tool in ("list_devices", "get_state", "set_state", "run_scene", "read_sensor") else "invalid"
        if err:
            self.metrics.reject(err)
            res = {"ok": False, "error": err, "rejected_by": "agent"}
            await ctx.emit("tool_call", {"call_id": call_id, "tool": tool_s, "args": _safe_args(shown_args)})
            await ctx.emit("tool_result", {"call_id": call_id, "tool": tool_s, "result": res, "ok": False})
            self._log(ctx, "rejected", tool_s, shown_args, False, {"error": err})
            return CallOutcome(call_id, tool_s, shown_args, res, False, "rejected", note=err)
        if not ctx.budget.take_call():
            res = {"ok": False, "error": "budget_exceeded"}
            self._log(ctx, "budget_exceeded", tool, args, False, None)
            return CallOutcome(call_id, tool, args, res, False, "budget", note="budget_exceeded")

        risk = "normal"
        if tool == "set_state":
            d = self.registry.devices.get(args["endpoint_id"])
            risk = d.risk if d else "normal"
        decision = needs_confirmation(Action(tool, args, risk), ctx.trigger)
        if decision is Decision.NOTIFY_ONLY:
            return await self._blocked_by_trigger(ctx, call_id, tool, args)
        if decision is Decision.DENY:
            return await self._denied(ctx, call_id, tool, args)
        if decision is Decision.CONFIRM:                       # known risky: nothing is sent to the hub before approval
            return await self._confirm_and_run(ctx, call_id, tool, args, blocked=None, call_emitted=False)

        await ctx.emit("tool_call", {"call_id": call_id, "tool": tool, "args": _safe_args(args)})
        result = await self._hub_call(ctx, call_id, tool, args)
        blocked = _is_blocked(tool, result)
        if blocked:
            await ctx.emit("tool_result", {"call_id": call_id, "tool": tool, "result": clean_result(tool, result), "ok": False})
            self.settles.cancel(call_id)
            d2 = needs_confirmation(Action(tool, args, "confirm"), ctx.trigger)   # the hub knows a risk we did not
            if d2 is Decision.NOTIFY_ONLY:
                return await self._blocked_by_trigger(ctx, call_id, tool, args, announce=False)
            if d2 is Decision.DENY:
                return await self._denied(ctx, call_id, tool, args, announce=False)
            return await self._confirm_and_run(ctx, call_id, tool, args, blocked=result, call_emitted=True)
        return await self._finish(ctx, call_id, tool, args, result)

    async def run_approved(self, ctx: ReqCtx, action: dict, token: str) -> CallOutcome:
        """Approval hand-off without a waiting turn (a Retry request the agent never saw, or its stream is gone): run the
        exact approved action with the gateway-minted token attached out-of-band. The hub still validates and consumes the
        token; nothing here can widen the action."""
        call_id = "c_" + secrets.token_hex(8)
        if action.get("type") == "set_state":
            tool, raw = "set_state", {k: action[k] for k in ("endpoint_id", "capability", "value")}
        else:
            tool, raw = "run_scene", {"scene": action.get("scene_id")}
        err, args = validate_call(self.registry, tool, raw)
        if err:
            self.metrics.reject(err)
            res = {"ok": False, "error": err, "rejected_by": "agent"}
            self._log(ctx, "rejected", tool, raw, False, {"error": err})
            return CallOutcome(call_id, tool, raw, res, False, "rejected", note=err)
        if not ctx.budget.take_call():
            return CallOutcome(call_id, tool, args, {"ok": False, "error": "budget_exceeded"}, False, "budget")
        self._log(ctx, "confirmation_approved", tool, args, None, None)
        await ctx.emit("tool_call", {"call_id": call_id, "tool": tool, "args": _safe_args(args)})
        result = await self._hub_call(ctx, call_id, tool, args, token=token)
        if _is_blocked(tool, result):
            await ctx.emit("tool_result", {"call_id": call_id, "tool": tool, "result": clean_result(tool, result), "ok": False})
            self.settles.cancel(call_id)
            self._log(ctx, "confirmation_expired", tool, args, False, {"reason": "token_expired"})
            return CallOutcome(call_id, tool, args, {"ok": False, "error": "confirmation_expired"}, False, "expired",
                               note="token_expired")
        return await self._finish(ctx, call_id, tool, args, result)

    # ------------------------------------------------------------------------------------------ hub call
    async def _hub_call(self, ctx: ReqCtx, call_id: str, tool: str, args: dict, token: str | None = None) -> dict:
        meta = {"call_id": call_id}
        if ctx.trigger.is_user:
            meta["user_id"] = ctx.user_id
        if token:
            meta["confirmation_token"] = token
        motion = tool in ("set_state", "run_scene")
        if motion:
            self.settles.expect(call_id, ctx)                    # before the call: a fast settle must not be lost
        while True:                                               # armed or cancelled in _finish / on a block
            try:
                result = await self.hub.call_tool(tool, args, meta)
            except HubUnavailable as e:
                log.warning("hub call %s failed: %s", tool, e)
                result = {"ok": False, "error": "hub_unavailable"}
            if (tool == "set_state" and result.get("error") in RETRYABLE
                    and ctx.budget.take_retry()):                # desired-state commands are safe to repeat
                continue
            return result

    async def _finish(self, ctx: ReqCtx, call_id: str, tool: str, args: dict, result: dict) -> CallOutcome:
        ok = result.get("ok", True) is not False and "error" not in result
        status = result.get("status") if isinstance(result.get("status"), str) else None
        moving: set[tuple[str, str]] = set()
        if ok and tool == "set_state" and status == "moving":
            moving.add((args["endpoint_id"], args["capability"]))
        if tool == "run_scene" and isinstance(result.get("results"), list):
            for r in result["results"]:
                if isinstance(r, dict) and r.get("status") == "moving" and isinstance(r.get("endpoint_id"), str):
                    moving.add((r["endpoint_id"], str(r.get("capability"))))
        if tool in ("set_state", "run_scene"):
            await self.settles.arm(call_id, moving)
        verified = result.get("verified") if tool == "set_state" and ok else None
        state = "moving" if moving else ("done" if ok else "failed")
        if tool == "run_scene" and ok and any(isinstance(r, dict) and r.get("status") in ("failed",) for r in result.get("results", [])):
            ok = False
        cleaned = clean_result(tool, result)
        await ctx.emit("tool_result", {"call_id": call_id, "tool": tool, "result": cleaned, "ok": ok})
        if tool not in READ_TOOLS:
            self.metrics.calls_executed += 1
        self._log(ctx, state, tool, args, ok, {"result": cleaned, "verified": verified})
        return CallOutcome(call_id, tool, args, cleaned, ok, state, verified,
                           note="" if verified is not False else "unverified")

    # ------------------------------------------------------------------------------------------ confirmation
    async def _confirm_and_run(self, ctx: ReqCtx, call_id: str, tool: str, args: dict, blocked: dict | None,
                               call_emitted: bool) -> CallOutcome:
        action, summary = self._action_for_confirmation(tool, args, blocked)
        if tool == "set_state":
            h = action_hash(action)
        else:                                     # the hub's scene_id + canonical list + hash; we verify, never invent
            h = blocked.get("action_hash") if isinstance(blocked, dict) else None
            if (not isinstance(action.get("scene_id"), str) or not action["actions"] or not isinstance(h, str)
                    or action_hash(action) != h):
                log.error("run_scene needs_confirmation from the hub is malformed or its hash does not match ours")
                self.settles.cancel(call_id)
                self._log(ctx, "failed", tool, args, False, {"error": "bad_tool_result"})
                return CallOutcome(call_id, tool, args, {"ok": False, "error": "bad_tool_result"}, False, "failed",
                                   note="bad_tool_result")
        self._log(ctx, "confirmation_requested", tool, args, None, {"summary": summary})
        out = await self.confirmations.request(ctx, summary, action, h)
        if out.kind == "declined":
            self._log(ctx, "confirmation_declined", tool, args, False, None)
            return CallOutcome(call_id, tool, args, {"ok": False, "error": "confirmation_declined"}, False, "declined",
                               note="declined")
        if out.kind == "expired" or not out.token:
            self._log(ctx, "confirmation_expired", tool, args, False, {"reason": out.reason or "no_response"})
            return CallOutcome(call_id, tool, args, {"ok": False, "error": "confirmation_expired"}, False, "expired",
                               note="expired")
        self._log(ctx, "confirmation_approved", tool, args, None, None)
        if not call_emitted:
            await ctx.emit("tool_call", {"call_id": call_id, "tool": tool, "args": _safe_args(args)})
        result = await self._hub_call(ctx, call_id, tool, args, token=out.token)
        out.token = None                                           # single use; drop our copy
        if _is_blocked(tool, result):
            await ctx.emit("tool_result", {"call_id": call_id, "tool": tool, "result": clean_result(tool, result), "ok": False})
            await self.confirmations.token_expired(ctx, out)
            self.settles.cancel(call_id)
            self._log(ctx, "confirmation_expired", tool, args, False, {"reason": "token_expired"})
            return CallOutcome(call_id, tool, args, {"ok": False, "error": "confirmation_expired"}, False, "expired",
                               note="token_expired")
        return await self._finish(ctx, call_id, tool, args, result)

    def _action_for_confirmation(self, tool: str, args: dict, blocked: dict | None) -> tuple[dict, str]:
        if tool == "set_state":
            d = self.registry.devices.get(args["endpoint_id"])
            name = d.name if d else args["endpoint_id"]
            v = args["value"]
            if args["capability"] == "lock" and v in ("locked", "unlocked"):
                summary = f"{'Unlock' if v == 'unlocked' else 'Lock'} {name}?"
            else:
                shown = ("on" if v else "off") if isinstance(v, bool) else v
                summary = f"Set {name} {args['capability']} to {shown}?"
            return ({"type": "set_state", "endpoint_id": args["endpoint_id"], "capability": args["capability"],
                     "value": v}, summary)
        need = _canonical_actions((blocked or {}).get("needs_confirmation"))
        names = ", ".join(sorted({self.registry.devices[n["endpoint_id"]].name for n in need if n["endpoint_id"] in self.registry.devices})[:4])
        summary = f"Run scene '{clean_label(args['scene'], 48)}'?" + (f" It affects: {names}." if names else "")
        return ({"type": "run_scene", "scene_id": (blocked or {}).get("scene_id"), "actions": need}, summary)

    async def _blocked_by_trigger(self, ctx: ReqCtx, call_id: str, tool: str, args: dict, announce: bool = True) -> CallOutcome:
        """Proactive/automation trigger on a risk:confirm action: nothing executes, no prompt, only a notification."""
        text = (f"{_trigger_label(ctx)} wanted to run {_describe(tool, args, self.registry)}. "
                "Needs your confirmation; open the app to do it yourself.")
        await self.notifier.notify(ctx.user_id if ctx.trigger.is_user else None, "action_blocked", text)
        self.settles.cancel(call_id)
        self._log(ctx, "blocked_by_trigger", tool, args, False, None)
        res = {"ok": False, "error": "blocked_notify_only"}
        if announce:
            await ctx.emit("tool_result", {"call_id": call_id, "tool": tool, "result": res, "ok": False})
        return CallOutcome(call_id, tool, args, res, False, "blocked", note="notify_only")

    async def _denied(self, ctx: ReqCtx, call_id: str, tool: str, args: dict, announce: bool = True) -> CallOutcome:
        self.settles.cancel(call_id)
        self._log(ctx, "denied_guest", tool, args, False, None)
        res = {"ok": False, "error": "not_allowed"}
        if announce:
            await ctx.emit("tool_result", {"call_id": call_id, "tool": tool, "result": res, "ok": False})
        return CallOutcome(call_id, tool, args, res, False, "denied", note="not_allowed")

    # ------------------------------------------------------------------------------------------ action log
    def _log(self, ctx: ReqCtx, status: str, tool: str | None, args: dict | None, ok: bool | None, detail: dict | None) -> None:
        self.store.log_action(user_id=ctx.user_id if ctx.trigger.is_user else None, req_id=ctx.req_id,
                              trigger_kind=ctx.trigger.kind, trigger_ref=ctx.trigger.ref or None, reason=ctx.reason,
                              tool=tool, args=_safe_args(args) if args else None, status=status, ok=ok, detail=detail)


# ----------------------------------------------------------------------------------------------------- helpers
def _canonical_actions(raw: Any) -> list[dict]:
    """The hub's canonical risk:confirm list: exactly endpoint_id, capability, value; sorted by endpoint_id then capability."""
    out = []
    for n in raw if isinstance(raw, list) else []:
        if isinstance(n, dict) and isinstance(n.get("endpoint_id"), str) and n.get("capability") in _caps() \
                and isinstance(n.get("value"), (bool, int, float, str)):
            out.append({"endpoint_id": n["endpoint_id"], "capability": n["capability"], "value": n["value"]})
    return sorted(out, key=lambda x: (x["endpoint_id"], x["capability"]))


def _caps() -> set[str]:
    from .devices import VOCAB
    return set(VOCAB)


def _trigger_label(ctx: ReqCtx) -> str:
    return f"Rule '{clean_label(ctx.label or ctx.trigger.ref, 32)}'" if ctx.trigger.kind == "rule" else f"A {ctx.trigger.kind} trigger"


def _describe(tool: str, args: dict, reg: DeviceRegistry) -> str:
    if tool == "set_state":
        d = reg.devices.get(args.get("endpoint_id"))
        return f"set {d.name if d else 'a device'} {clean_label(args.get('capability'), 24)} to {_scalar(args.get('value'))}"
    if tool == "run_scene":
        return f"run scene '{clean_label(args.get('scene'), 48)}'"
    return tool


def _is_blocked(tool: str, result: dict) -> bool:
    return ((tool == "set_state" and result.get("error") == "confirmation_required")
            or (tool == "run_scene" and result.get("status") == "needs_confirmation"))


def _safe_args(args: dict | None) -> dict:
    return {k: v for k, v in (args or {}).items() if k not in ("confirmation_token", "user_id", "call_id", "_meta", "token")}


def clean_result(tool: str, r: dict) -> dict:
    """Whitelist + sanitise a hub result for SSE, the log and the model. Device-supplied free text cannot pass."""
    def s(v: object, n: int = 32) -> str | None:
        return clean_label(v, n) if isinstance(v, str) else None

    out: dict[str, Any] = {}
    if "ok" in r:
        out["ok"] = bool(r["ok"])
    if "error" in r:
        out["error"] = s(r["error"]) or "error"
    if tool == "set_state":
        for k in ("status", "endpoint_id", "capability"):
            if k in r:
                out[k] = s(r[k])
        if "verified" in r:
            out["verified"] = bool(r["verified"])
        if "value" in r:
            out["value"] = _scalar(r["value"])
    elif tool == "get_state":
        out["states"] = [{"endpoint_id": s(x.get("endpoint_id")), "online": bool(x.get("online")),
                          "values": {s(k, 24): _scalar(v) for k, v in list((x.get("values") or {}).items())[:16]},
                          "ts": x.get("ts") if isinstance(x.get("ts"), (int, float)) else None}
                         for x in (r.get("states") or [])[:40] if isinstance(x, dict)]
    elif tool == "read_sensor":
        for k in ("endpoint_id", "capability", "unit"):
            if k in r:
                out[k] = s(r[k], 24)
        out["value"] = _scalar(r.get("value"))
        out["stale"] = bool(r.get("stale"))
        out["ts"] = r.get("ts") if isinstance(r.get("ts"), (int, float)) else None
    elif tool == "list_devices":
        out["count"] = len(r.get("devices") or []) if isinstance(r.get("devices"), list) else 0
    elif tool == "run_scene":
        for k in ("status", "scene", "scene_name", "scene_id"):
            if k in r:
                out[k] = s(r[k], 48)
        out["executed"] = bool(r.get("executed"))
        out["results"] = [{"endpoint_id": s(x.get("endpoint_id")), "capability": s(x.get("capability"), 24),
                           "status": s(x.get("status"), 24), **({"detail": s(x.get("detail"), 64)} if x.get("detail") else {})}
                          for x in (r.get("results") or [])[:50] if isinstance(x, dict)]
        out["needs_confirmation"] = [{"endpoint_id": s(x.get("endpoint_id")), "capability": s(x.get("capability"), 24),
                                      "value": _scalar(x.get("value"))}
                                     for x in (r.get("needs_confirmation") or [])[:50] if isinstance(x, dict)]
    return out
