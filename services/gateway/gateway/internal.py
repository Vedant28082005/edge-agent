"""Internal listener (127.0.0.1:8001 or a unix socket): POST /internal/confirmations/consume ONLY.

Authenticated with the device-hub secret in X-Service-Secret, compared (SHA-256, constant time) with the verifier file
<verifier_dir>/device-hub.sha256. A missing verifier means every call is 401 (fail closed)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .context import Ctx
from .security import ct_equal, sha256_hex

log = logging.getLogger("gateway.internal")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
FIELDS = {"token", "user_id", "action_hash"}


def _err(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _verifier(ctx: Ctx) -> str | None:
    try:
        v = (Path(ctx.cfg.verifier_dir) / "device-hub.sha256").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return v if HASH_RE.match(v) else None


def create_internal_app(ctx: Ctx) -> FastAPI:
    app = FastAPI(title="gateway-internal", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/internal/confirmations/consume")
    async def consume(request: Request):
        presented = request.headers.get("x-service-secret", "")
        ver = _verifier(ctx)
        if not presented or ver is None or not ct_equal(sha256_hex(presented), ver):
            ctx.db.audit("unknown", "confirm.consume", result="unauthorized")
            return _err(401, "unauthorized")
        try:
            body = await request.json()
        except ValueError:
            return _err(400, "bad_request")
        if not isinstance(body, dict) or set(body) != FIELDS:
            return _err(400, "bad_request")
        token, uid, ah = body["token"], body["user_id"], body["action_hash"]
        if not (isinstance(token, str) and TOKEN_RE.match(token) and isinstance(ah, str) and HASH_RE.match(ah)
                and isinstance(uid, str) and 1 <= len(uid) <= 64):
            return _err(400, "bad_request")
        granted, out = ctx.confirm.consume(token, uid, ah)
        return JSONResponse(out, status_code=200 if granted else 403)

    return app
