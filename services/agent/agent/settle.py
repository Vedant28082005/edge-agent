"""Matches late device-hub settle events (`action_settled` / `action_settle_timeout`) to the request that started the
motion, by the executor's own `call_id` (echoed by the hub from `_meta.call_id`). The table lives only in memory: after
an agent restart nothing matches, and timeouts fall back to a notification (contracts/agent-events.md).

Race: a fast settle can arrive on the event stream before the tool response has been processed. So the executor calls
`expect(call_id)` BEFORE the call and `arm(call_id, targets)` after it; events for an expected-but-unarmed call are
buffered and replayed on `arm`."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .devices import VOCAB
from .notify import Notifier
from .reqctx import ReqCtx
from .util import EP_RE

log = logging.getLogger("agent.settle")


@dataclass
class _Pending:
    ctx: ReqCtx
    targets: set[tuple[str, str]] | None = None             # None until armed
    buffer: list[tuple[str, dict]] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)


def _scalar(v: object):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and len(v) <= 24 and all(c.isalnum() or c == "_" for c in v):
        return v
    return None


def sanitize_settle(kind: str, data: dict) -> dict | None:
    """Only registry/state-derived scalar fields pass; anything else is dropped."""
    ep, cap = data.get("endpoint_id"), data.get("capability")
    if not isinstance(ep, str) or not EP_RE.match(ep) or cap not in VOCAB:
        return None
    out = {"endpoint_id": ep, "capability": cap, "value": _scalar(data.get("value")),
           "observed": _scalar(data.get("observed"))}
    if kind == "action_settled":
        out["elapsed_ms"] = data["elapsed_ms"] if isinstance(data.get("elapsed_ms"), (int, float)) else None
    else:
        out["settle_s"] = data["settle_s"] if isinstance(data.get("settle_s"), (int, float)) else None
    return out


class SettleTable:
    def __init__(self, notifier: Notifier, ttl_s: float = 150.0) -> None:
        self._pending: dict[str, _Pending] = {}
        self._notifier, self._ttl = notifier, ttl_s
        self.unmatched = 0

    def __len__(self) -> int:
        return len(self._pending)

    def expect(self, call_id: str, ctx: ReqCtx) -> None:
        if call_id in self._pending:
            return
        self._pending[call_id] = _Pending(ctx)
        ctx.settle_added()

    async def arm(self, call_id: str, targets: set[tuple[str, str]]) -> None:
        """After the tool response: these (endpoint, capability) pairs are moving. Empty = nothing to wait for."""
        p = self._pending.get(call_id)
        if p is None:
            return
        if not targets:
            self._finish(call_id, p)
            return
        p.targets = set(targets)
        buffered, p.buffer = p.buffer, []
        for kind, data in buffered:
            await self._deliver(call_id, p, kind, data)

    def cancel(self, call_id: str) -> None:
        p = self._pending.get(call_id)
        if p is not None:
            self._finish(call_id, p)

    def _finish(self, call_id: str, p: _Pending) -> None:
        if self._pending.pop(call_id, None) is not None:
            p.ctx.settle_done()

    async def on_hub_event(self, kind: str, data: dict) -> bool:
        """kind: 'action_settled' | 'action_settle_timeout'. Returns True if matched to a request."""
        clean = sanitize_settle(kind, data)
        if clean is None:
            log.warning("settle event with unexpected fields ignored")
            return False
        call_id = data.get("call_id")
        p = self._pending.get(call_id) if isinstance(call_id, str) else None
        if p is not None and p.targets is None:
            p.buffer.append((kind, data))
            return True
        if p is None or (clean["endpoint_id"], clean["capability"]) not in (p.targets or set()):
            self.unmatched += 1
            if kind == "action_settle_timeout":
                await self._notifier.notify(None, "action_failed", _timeout_text(clean))
            return False
        await self._deliver(call_id, p, kind, data)
        return True

    async def _deliver(self, call_id: str, p: _Pending, kind: str, data: dict) -> None:
        clean = sanitize_settle(kind, data)
        key = (data.get("endpoint_id"), data.get("capability"))
        if clean is None or p.targets is None or key not in p.targets:
            if kind == "action_settle_timeout" and clean is not None:
                await self._notifier.notify(p.ctx.user_id, "action_failed", _timeout_text(clean))
            return
        delivered = await p.ctx.emit(kind, {"call_id": call_id, **clean})
        if not delivered and kind == "action_settle_timeout":
            await self._notifier.notify(p.ctx.user_id, "action_failed", _timeout_text(clean))
        p.targets.discard(key)
        if not p.targets:
            self._finish(call_id, p)

    def expire(self) -> int:
        """Drop entries older than the TTL (the hub's max settle_s is 120 s). Returns how many."""
        now, n = time.monotonic(), 0
        for cid, p in list(self._pending.items()):
            if now - p.created > self._ttl:
                self._finish(cid, p)
                n += 1
        return n


def _timeout_text(clean: dict) -> str:
    return f"A device did not reach its target ({clean['endpoint_id']} {clean['capability']})."
