"""Client for the gateway's internal POST /internal/confirmations/consume (openapi/gateway.yaml, internal section).

Fails closed: anything other than a well-formed 200 {consumed:true} that echoes our action_hash and user_id is a
denial. The token and the service secret are never logged (both are registered with the log redactor).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .util import SECRETS, TOKEN_RE

log = logging.getLogger("device_hub.gateway")

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DENY_REASONS = {"expired", "used", "hash_mismatch", "unknown"}


@dataclass
class ConsumeResult:
    granted: bool
    reason: str | None = None     # expired|used|hash_mismatch|unknown (gateway) or invalid|gateway_error (local)


class GatewayClient:
    def __init__(self, url: str, secret: str, timeout_s: float = 2.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        SECRETS.add(secret)
        self._secret = secret
        base = url
        if url.startswith("unix://"):
            path = url[len("unix://"):]
            transport = transport or httpx.HTTPTransport(uds=path)
            base = "http://gateway-internal"
        self._http = httpx.Client(base_url=base, timeout=timeout_s, transport=transport)

    @classmethod
    def from_secret_file(cls, url: str, secret_file: str, timeout_s: float = 2.0) -> "GatewayClient":
        p = Path(secret_file)
        try:
            if os.name != "nt" and p.stat().st_mode & 0o077:
                log.warning("secret file %s is accessible by group/others (expected mode 600)", p.name)
        except OSError:
            pass
        return cls(url, p.read_text(encoding="utf-8").strip(), timeout_s)

    def consume(self, token: str, user_id: str, action_hash: str) -> ConsumeResult:
        if not (isinstance(token, str) and TOKEN_RE.match(token)):
            return ConsumeResult(False, "invalid")
        SECRETS.add(token, permanent=False)
        if not (isinstance(user_id, str) and 1 <= len(user_id) <= 64) or not HASH_RE.match(action_hash):
            return ConsumeResult(False, "invalid")
        try:
            r = self._http.post("/internal/confirmations/consume",
                                json={"token": token, "user_id": user_id, "action_hash": action_hash},
                                headers={"X-Service-Secret": self._secret})
        except httpx.HTTPError as e:
            log.warning("gateway consume failed: %s", type(e).__name__)
            return ConsumeResult(False, "gateway_error")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200:
            if body.get("consumed") is True and body.get("action_hash") == action_hash and body.get("user_id") == user_id:
                return ConsumeResult(True)
            log.warning("gateway granted with a mismatching body; treating as denied")
            return ConsumeResult(False, "gateway_error")
        if r.status_code == 403:
            reason = body.get("reason")
            return ConsumeResult(False, reason if reason in DENY_REASONS else "gateway_error")
        log.warning("gateway consume returned HTTP %s", r.status_code)   # 401 = wrong/unscoped secret
        return ConsumeResult(False, "gateway_error")

    def close(self) -> None:
        self._http.close()


class NoGateway:
    """Used when no gateway is configured: every consume is denied (risk:confirm actions stay blocked)."""

    def consume(self, token: str, user_id: str, action_hash: str) -> ConsumeResult:
        return ConsumeResult(False, "gateway_error")

    def close(self) -> None:
        pass


ConsumeFn = Callable[[str, str, str], ConsumeResult]
