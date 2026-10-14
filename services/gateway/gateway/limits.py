"""Per-user request limits (429 + Retry-After) and single-use WebSocket tickets."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from .security import SECRETS, new_token, sha256_hex


class RateLimiter:
    """Sliding window per (name, key)."""

    def __init__(self, limits: dict, clock=time.time) -> None:
        self.limits, self.clock = limits, clock
        self._q: dict[tuple, deque] = defaultdict(deque)

    def hit(self, name: str, key: str) -> int:
        """Count one request. Returns 0 if allowed, else the Retry-After seconds (the request is NOT counted then)."""
        mx, window = self.limits.get(name, (1_000_000, 60))
        q, now = self._q[(name, key)], self.clock()
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= mx:
            return max(1, int(q[0] + window - now) + 1)
        q.append(now)
        return 0


class WsTickets:
    """POST /ws/ticket: opaque 256-bit, single use (burned on first presentation, valid or not), bound to the user, short TTL.
    Only the hash is held. `access_exp` ties the socket's life to the access token that minted the ticket."""

    def __init__(self, ttl_s: int = 30, clock=time.time, max_pending_per_user: int = 8) -> None:
        self.ttl, self.clock, self.max_pending = ttl_s, clock, max_pending_per_user
        self._t: dict[str, tuple[str, float, float]] = {}

    def _sweep(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._t.items() if v[1] <= now]:
            del self._t[k]

    def mint(self, user_id: str, access_exp: float) -> str | None:
        self._sweep()
        if sum(1 for v in self._t.values() if v[0] == user_id) >= self.max_pending:
            return None
        tok = new_token()
        SECRETS.add(tok)
        self._t[sha256_hex(tok)] = (user_id, self.clock() + self.ttl, access_exp)
        return tok

    def take(self, ticket: str) -> tuple[str, float] | None:
        """(user_id, access_exp) or None. The ticket is gone afterwards either way."""
        self._sweep()
        v = self._t.pop(sha256_hex(ticket), None)
        return (v[0], v[2]) if v else None
