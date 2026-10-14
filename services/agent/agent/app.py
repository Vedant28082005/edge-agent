"""HTTP API of the agent (internal, 127.0.0.1:8200): POST /v1/chat (SSE), GET /health, GET/POST /v1/runtime and
GET /v1/stats (dev/eval switches, not part of the user-facing contract; loopback only, nothing here reaches the LLM)."""
from __future__ import annotations

import json
import re
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .service import Agent

ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")


def sse(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


def create_app(agent: Agent, mock_confirm: Callable[[str, str, bool], bool] | None = None) -> Starlette:
    async def chat(request: Request):
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "bad_request", "message": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request", "message": "body must be an object"}, status_code=400)
        user_id, session_id, text = body.get("user_id"), body.get("session_id"), body.get("text")
        if not (isinstance(user_id, str) and ID_RE.match(user_id) and isinstance(session_id, str) and ID_RE.match(session_id)):
            return JSONResponse({"error": "bad_request", "message": "user_id and session_id are required"}, status_code=400)
        if body.get("audio_ref") or body.get("image_refs"):
            return JSONResponse({"error": "bad_request", "message": "audio_ref and image_refs are not supported in v1 "
                                 "(speech-to-text and vision are S6/S8)"}, status_code=400)
        if not isinstance(text, str) or not text.strip() or len(text) > agent.cfg.max_text_chars:
            return JSONResponse({"error": "bad_request", "message": "text is required (max %d chars)" % agent.cfg.max_text_chars},
                                status_code=400)
        ctx = agent.new_request(user_id, session_id, text)

        async def gen():
            try:
                while True:
                    item = await ctx.out.get()
                    if item is None:
                        return
                    yield sse(*item)
            finally:
                ctx.detached = True            # client gone: later settle outcomes go to the notifier

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def health(_: Request):
        return JSONResponse({"ok": True, "devices": len(agent.registry.devices),
                             "hub_stream": bool(agent.stream and agent.stream.connected)})

    async def stats(_: Request):
        return JSONResponse(agent.stats())

    async def runtime(request: Request):
        if request.method == "POST":
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                body = None
            if not isinstance(body, dict) or not isinstance(body.get("constrained"), bool):
                return JSONResponse({"error": "bad_request", "message": "body must be {\"constrained\": bool}"}, status_code=400)
            agent.llm.constrained = body["constrained"]
        return JSONResponse({"constrained": agent.llm.constrained})

    routes = [Route("/v1/chat", chat, methods=["POST"]), Route("/health", health), Route("/v1/stats", stats),
              Route("/v1/runtime", runtime, methods=["GET", "POST"])]

    if mock_confirm is not None:                       # --mock only: stands in for gateway POST /confirmations
        async def mock_confirmation(request: Request):
            body = await request.json()
            ok = mock_confirm(body.get("request_id", ""), body.get("user_id", ""), bool(body.get("approve")))
            return JSONResponse({"resolved": ok})

        routes.append(Route("/mock/confirm", mock_confirmation, methods=["POST"]))
    return Starlette(routes=routes)
