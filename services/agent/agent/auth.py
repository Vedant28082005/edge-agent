"""Mandatory gateway-to-agent authentication (contracts/openapi/agent-internal.yaml).

The gateway sends its secret in `X-Service-Secret`; the agent holds only the SHA-256 verifier (hex) of it, in a file
(default /etc/edge-agent/secrets/verifiers/agent-api.sha256), and compares in constant time. The secret never reaches
the agent's disk, env, argv or logs. Every operation needs it, /v1/health included. This secret is unrelated to the
agent's hub secret (agent.secret). The only way to run without it is `--mock --insecure-no-auth`."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ApiAuth:
    def __init__(self, verifier_file: str) -> None:
        p = Path(verifier_file)
        try:
            v = p.read_text(encoding="utf-8").strip().lower()
        except OSError as e:
            raise SystemExit(f"api verifier file {verifier_file!r} is not readable ({type(e).__name__}): "
                             "refusing to start without caller authentication (--mock --insecure-no-auth for a local mock)")
        if not HEX64.match(v):
            raise SystemExit(f"api verifier file {verifier_file!r} must hold a SHA-256 hex digest")
        self._verifier = v

    @classmethod
    def from_secret(cls, secret: str, path: str) -> "ApiAuth":
        """Mock/test helper: write the verifier for `secret` to `path` and load it."""
        Path(path).write_text(hashlib.sha256(secret.encode()).hexdigest(), encoding="utf-8")
        return cls(path)

    def check(self, presented: str | None) -> bool:
        if not presented or len(presented) > 512:
            return False
        return hmac.compare_digest(hashlib.sha256(presented.encode("utf-8", "ignore")).hexdigest(), self._verifier)


class AuthMiddleware:
    """Pure ASGI (does not buffer streams). 401 JSON for a missing/wrong secret, before any routing or body read."""

    def __init__(self, app, auth: ApiAuth | None) -> None:
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.auth is not None:
            presented = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-service-secret"), None)
            if not self.auth.check(presented):
                body = json.dumps({"error": "unauthorized", "message": "missing or wrong X-Service-Secret"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
