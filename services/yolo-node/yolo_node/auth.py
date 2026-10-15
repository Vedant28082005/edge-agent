"""Mandatory caller authentication (contracts/openapi/yolo-node-internal.yaml, scope `agent`).

The agent sends its secret in `X-Service-Secret`; this node holds only the SHA-256 verifier (hex) in a file
(default /etc/edge-agent/secrets/verifiers/agent.sha256) and compares in constant time. Every operation except
GET /health needs it, checked before routing and before any body byte is read. The only way to run without it is
`--mock --insecure-no-auth`."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Optional

from .errors import NodeError

HEX64 = re.compile(r"^[0-9a-f]{64}$")
OPEN_PATHS = {"/health"}


class ApiAuth:
    def __init__(self, verifier_file: str) -> None:
        try:
            v = Path(verifier_file).read_text(encoding="utf-8").strip().lower()
        except OSError as e:
            raise SystemExit("verifier file %r is not readable (%s): refusing to start without caller authentication "
                             "(--mock --insecure-no-auth for a local mock)" % (verifier_file, type(e).__name__))
        if not HEX64.match(v):
            raise SystemExit("verifier file %r must hold a SHA-256 hex digest" % verifier_file)
        self._verifier = v

    @classmethod
    def from_secret(cls, secret: str, path: str) -> "ApiAuth":
        """Test helper: write the verifier for `secret` to `path` and load it."""
        Path(path).write_text(hashlib.sha256(secret.encode()).hexdigest(), encoding="utf-8")
        return cls(path)

    def check(self, presented: Optional[str]) -> bool:
        if not presented or len(presented) > 512:
            return False
        return hmac.compare_digest(hashlib.sha256(presented.encode("utf-8", "ignore")).hexdigest(), self._verifier)


class AuthMiddleware:
    """Pure ASGI. 401 JSON for a missing/wrong secret on everything except /health."""

    def __init__(self, app, auth: Optional[ApiAuth], on_reject=None) -> None:
        self.app, self.auth, self.on_reject = app, auth, on_reject

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.auth is not None and scope["path"] not in OPEN_PATHS:
            presented = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-service-secret"), None)
            if not self.auth.check(presented):
                if self.on_reject:
                    self.on_reject()
                body = json.dumps(NodeError("unauthorized").body()).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
