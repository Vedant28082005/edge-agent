"""Confirmation flow (contracts/agent-events.md v6, mcp-tools.md v5).

The agent can NEVER mint or hold a token it was not given. Sequence:
  1. a user-triggered action hits a `risk: confirm` endpoint; nothing has been executed;
  2. `request()` emits `confirmation_request` (summary + the exact action + action_hash + expires_in_s) and waits;
  3. the user approves in the UI -> gateway `POST /confirmations` mints the token -> the gateway calls the agent's
     `POST /v1/confirmations/{request_id}` (contracts/openapi/agent-internal.yaml), handled in service.py, which ends in
     `resolve()` below. declined/expired decisions arrive the same way and clear the pending state.
  4. on a token, the executor re-issues the same call with the token in MCP `_meta` (never in args/events/logs);
     decline -> `confirmation_declined`; no answer within `expires_in_s` -> `confirmation_expired` (no_response).
There is no automatic re-prompt and no retry here (Retry is a new request started by the user)."""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field

from .reqctx import ReqCtx

log = logging.getLogger("agent.confirm")


@dataclass
class Outcome:
    kind: str                                   # approved | declined | expired
    token: str | None = field(default=None, repr=False)     # never printed
    reason: str = ""
    request_id: str = ""
    summary: str = ""


@dataclass
class _Pending:
    fut: asyncio.Future
    user_id: str
    summary: str
    action: dict
    action_hash: str | None = None


class ConfirmationManager:
    def __init__(self, ttl_s: float = 60.0) -> None:
        self.ttl_s = ttl_s
        self._pending: dict[str, _Pending] = {}
        self._handled: OrderedDict[str, None] = OrderedDict()      # finished request ids (a repeated decision is a no-op)

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    def pending_hash(self, request_id: str, user_id: str) -> str | None:
        p = self._pending.get(request_id)
        return p.action_hash if p and p.user_id == user_id else None

    def owner_of(self, request_id: str) -> str | None:
        p = self._pending.get(request_id)
        return p.user_id if p else None

    def expire(self, request_id: str, user_id: str) -> bool:
        """The gateway reports the request expired (TTL passed, or the token died before consumption)."""
        p = self._pending.get(request_id)
        if p is None or p.fut.done() or p.user_id != user_id:
            return False
        p.fut.set_result(Outcome("expired", reason="no_response"))
        return True

    def is_handled(self, request_id: str) -> bool:
        return request_id in self._handled

    def knows(self, request_id: str, user_id: str) -> bool:
        p = self._pending.get(request_id)
        return bool(p and p.user_id == user_id)

    def pending_action(self, request_id: str) -> dict | None:
        p = self._pending.get(request_id)
        return dict(p.action) if p else None

    async def request(self, ctx: ReqCtx, summary: str, action: dict, action_hash: str | None) -> Outcome:
        request_id = "cr_" + secrets.token_hex(8)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _Pending(fut, ctx.user_id, summary, action, action_hash)
        await ctx.emit("confirmation_request", {"request_id": request_id, "summary": summary, "action": action,
                                                "action_hash": action_hash, "expires_in_s": int(self.ttl_s)})
        t0 = asyncio.get_running_loop().time()
        try:
            out: Outcome = await asyncio.wait_for(fut, self.ttl_s)
        except asyncio.TimeoutError:
            out = Outcome("expired", reason="no_response")
        finally:
            self._pending.pop(request_id, None)
            self._handled[request_id] = None
            while len(self._handled) > 500:
                self._handled.popitem(last=False)
            ctx.budget.paused_s += asyncio.get_running_loop().time() - t0      # waiting for a human is not LLM time
        out.request_id, out.summary = request_id, summary
        if out.kind == "declined":
            await ctx.emit("confirmation_declined", {"request_id": request_id, "summary": summary})
        elif out.kind == "expired":
            await ctx.emit("confirmation_expired", {"request_id": request_id, "summary": summary,
                                                    "reason": out.reason or "no_response"})
        return out

    def resolve(self, request_id: str, user_id: str, approved: bool, token: str | None = None) -> bool:
        """Deliver the user's decision (and, on approval, the gateway-minted token). Only the user who received the
        request may resolve it. Returns False if the request is unknown/finished or belongs to another user."""
        p = self._pending.get(request_id)
        if p is None or p.fut.done() or p.user_id != user_id:
            return False
        if approved and not token:
            return False
        p.fut.set_result(Outcome("approved", token=token) if approved else Outcome("declined"))
        return True

    async def token_expired(self, ctx: ReqCtx, out: Outcome) -> None:
        """The user approved but the hub no longer accepted the token (60 s TTL, used, mismatch)."""
        await ctx.emit("confirmation_expired", {"request_id": out.request_id, "summary": out.summary,
                                                "reason": "token_expired"})
