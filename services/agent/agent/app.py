"""The agent's HTTP API, exactly per contracts/openapi/agent-internal.yaml (1.3.0): loopback only, ONE caller (the
gateway), authentication mandatory on every operation including /v1/health (auth.py).

  POST /v1/chat                       SSE turn (role from the authenticated gateway; inline images; no audio/refs)
  POST /v1/confirmations/{request_id} confirmation hand-off: approved (hash recomputed) / declined / expired
  GET  /v1/events                     agent-originated events for the gateway, Last-Event-ID replay, `reset`
  GET  /v1/memory?user_id=            one user's memories
  PUT|DELETE /v1/memory/{memory_id}   edit / delete one memory, only if it belongs to user_id (else 404)
  GET  /v1/health                     {ok, llm, hub}
Extras, also authenticated and loopback-only (not part of the contract, for S10 and operators): GET /v1/stats,
GET|POST /v1/runtime {"constrained"|"fast_path"|"router"|"prefix_cache": bool}.

`role` and `user_id` are trusted only because the caller is the authenticated gateway. Request bodies are validated with
the contract's schemas (additionalProperties: false), and a confirmation token is never logged or echoed."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re

import jsonschema
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .auth import ApiAuth, AuthMiddleware
from .scope import MISSING, make_scope
from .sched.images import NodeImage, image_ref
from .service import Agent

log = logging.getLogger("agent.api")

MAX_BODY = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ROLE = {"type": "string", "enum": ["admin", "member", "guest"]}
VALUE = {"oneOf": [{"type": "boolean"}, {"type": "number"}, {"type": "string"}]}
ACTION_ITEM = {"type": "object", "additionalProperties": False, "required": ["endpoint_id", "capability", "value"],
               "properties": {"endpoint_id": {"type": "string", "pattern": "^ep_[0-9a-f]{8}$"},
                              "capability": {"type": "string", "maxLength": 64}, "value": VALUE}}
CHAT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["user_id", "role", "session_id"],
    "properties": {
        "user_id": {"type": "string", "minLength": 1, "maxLength": 64}, "role": ROLE,
        "session_id": {"type": "string", "maxLength": 64}, "text": {"type": "string", "maxLength": 4000},
        "allowed_rooms": {"type": ["array", "null"], "maxItems": 64, "uniqueItems": True,
                          "items": {"type": "string", "minLength": 1, "maxLength": 48}},
        "allowed_scenes": {"type": ["array", "null"], "maxItems": 200, "uniqueItems": True,
                           "items": {"type": "string", "minLength": 1, "maxLength": 64}},
        "images": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False, "required": ["ref", "media_type", "data_b64"],
            "properties": {"ref": {"type": "string", "maxLength": 64},
                           "media_type": {"enum": ["image/jpeg", "image/png", "image/webp"]},
                           "data_b64": {"type": "string"}}}}}}
DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decision", "user_id", "role", "action", "action_hash"],
    "properties": {
        "decision": {"enum": ["approved", "declined", "expired"]},
        "user_id": {"type": "string", "minLength": 1, "maxLength": 64}, "role": ROLE,
        "action": {"oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["type", "endpoint_id", "capability", "value"],
             "properties": {"type": {"const": "set_state"}, "endpoint_id": ACTION_ITEM["properties"]["endpoint_id"],
                            "capability": {"type": "string", "maxLength": 64}, "value": VALUE}},
            {"type": "object", "additionalProperties": False, "required": ["type", "scene_id", "actions"],
             "properties": {"type": {"const": "run_scene"}, "scene_id": {"type": "string", "maxLength": 64},
                            "actions": {"type": "array", "maxItems": 50, "items": ACTION_ITEM}}}]},
        "action_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "confirmation_token": {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}},
    "allOf": [{"if": {"properties": {"decision": {"const": "approved"}}},
               "then": {"required": ["confirmation_token"]}, "else": {"not": {"required": ["confirmation_token"]}}}]}
UPDATE_MEMORY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["user_id", "text"],
                        "properties": {"user_id": {"type": "string", "minLength": 1, "maxLength": 64},
                                       "text": {"type": "string", "minLength": 1, "maxLength": 500}}}
_V = {k: jsonschema.Draft202012Validator(v) for k, v in
      (("chat", CHAT_SCHEMA), ("decision", DECISION_SCHEMA), ("memory", UPDATE_MEMORY_SCHEMA))}
ID64 = re.compile(r"^[^\x00-\x1f]{1,64}$")
MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",), "image/webp": (b"RIFF",)}


def sse(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


def err(status: int, code: str, message: str = "", headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


async def read_json(request: Request, kind: str):
    """(body, None) or (None, error response). Never echoes the body (it may hold a confirmation token)."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return None, err(400, "bad_request", "body too large")
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, err(400, "bad_request", "body must be JSON")
    errors = list(_V[kind].iter_errors(body))
    if errors:
        path = "/".join(str(p) for p in errors[0].path) or "body"
        return None, err(400, "bad_request", f"invalid {path}")           # field path only, never the value
    return body, None


def check_images(images: list[dict]) -> str | None:
    for im in images:
        try:
            raw = base64.b64decode(im["data_b64"], validate=True)
        except (binascii.Error, ValueError):
            return "image data is not valid base64"
        if len(raw) > MAX_IMAGE_BYTES:
            return "image larger than 5 MiB"
        if not any(raw.startswith(m) for m in MAGIC[im["media_type"]]) or \
                (im["media_type"] == "image/webp" and raw[8:12] != b"WEBP"):
            return "image content does not match media_type"
    return None


def decode_images(images: list[dict]) -> list[NodeImage]:
    """Validated inline images -> RAM-only `NodeImage`s (call after `check_images`). `ref` is mapped to the nodes' stricter pattern."""
    return [NodeImage(image_ref(im.get("ref"), i), im["media_type"], base64.b64decode(im["data_b64"], validate=True))
            for i, im in enumerate(images)]


def create_app(agent: Agent, auth: ApiAuth | None) -> Starlette:
    """`auth=None` only for `--mock --insecure-no-auth` (enforced by the CLI) and tests."""

    async def chat(request: Request):
        body, bad = await read_json(request, "chat")
        if bad:
            return bad
        text, images = body.get("text", ""), body.get("images") or []
        if not text.strip() and not images:
            return err(400, "bad_request", "text or images required")
        if len(text) > agent.cfg.max_text_chars:
            return err(400, "bad_request", "text too long")
        msg = check_images(images)
        if msg:
            return err(400, "bad_request", msg)
        if agent.chat.busy():
            return err(429, "llm_busy", "the assistant is busy", {"Retry-After": "5"})
        # Fail closed (scope.py): a missing field is [] for member/guest; null is unrestricted only for admin/member.
        scope = make_scope(body["role"], body.get("allowed_rooms", MISSING), body.get("allowed_scenes", MISSING))
        ctx = agent.new_request(body["user_id"], body["session_id"], text, body["role"], len(images), scope,
                                **({"image_data": decode_images(images)} if images else {}))
        body = images = None                   # the request copy of the base64 is not kept past this point

        async def gen():
            try:
                while True:
                    item = await ctx.out.get()
                    if item is None:
                        return
                    yield sse(*item)
            finally:
                ctx.detached = True            # client gone: later events go to the gateway event ring
                ctx.cancel()                   # (S8o) aborted before `final`: stop the LLM wait and the remaining tool calls

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def confirmation(request: Request):
        request_id = request.path_params["request_id"]
        if not ID64.match(request_id):
            return err(400, "bad_request", "bad request_id")
        body, bad = await read_json(request, "decision")
        if bad:
            return bad
        status, payload = await agent.handle_decision(request_id, body)
        body = None
        return JSONResponse(payload, status_code=status)

    async def events(request: Request):
        last = request.headers.get("last-event-id")
        q = agent.ring.subscribe()
        reset, replay = agent.ring.replay_from(last)
        sent = agent.ring.last_id           # taken with the replay snapshot: anything newer arrives via the queue

        async def gen():
            nonlocal sent
            try:
                if reset:
                    yield b"event: reset\ndata: {}\n\n"
                for e in replay:
                    yield f"id: {e.id}\nevent: {e.name}\ndata: {json.dumps(e.data, separators=(',', ':'))}\n\n".encode()
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), 15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    if e.id > sent:
                        sent = e.id
                        yield f"id: {e.id}\nevent: {e.name}\ndata: {json.dumps(e.data, separators=(',', ':'))}\n\n".encode()
            finally:
                agent.ring.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    def uid(request: Request) -> str | None:
        v = request.query_params.get("user_id")
        return v if v and len(v) <= 64 else None

    async def memories(request: Request):
        u = uid(request)
        if not u:
            return err(400, "bad_request", "user_id is required")
        return JSONResponse(agent.store.list_memories(u))

    async def memory_item(request: Request):
        mid = request.path_params["memory_id"]
        if not ID64.match(mid):
            return err(400, "bad_request", "bad memory_id")
        if request.method == "DELETE":
            u = uid(request)
            if not u:
                return err(400, "bad_request", "user_id is required")
            if not agent.store.delete_memory(u, mid):
                return err(404, "not_found", "no such memory")
            return JSONResponse(None, status_code=204)
        body, bad = await read_json(request, "memory")
        if bad:
            return bad
        m = agent.store.update_memory(body["user_id"], mid, body["text"])
        return JSONResponse(m) if m else err(404, "not_found", "no such memory")

    async def health(_: Request):
        return JSONResponse(await agent.health())

    async def stats(_: Request):
        return JSONResponse(agent.stats())

    async def runtime(request: Request):
        if request.method == "POST":
            try:
                body = json.loads(await request.body())
            except (json.JSONDecodeError, ValueError):
                body = None
            keys = {"constrained", "constrained_decoding", "fast_path", "router", "prefix_cache"}
            if (not isinstance(body, dict) or not body or not set(body) <= keys
                    or not all(isinstance(v, bool) for v in body.values())):
                return err(400, "bad_request", "body must be a JSON object of booleans: " + ", ".join(sorted(keys)))
            agent.set_flags(**body)
        return JSONResponse({"constrained": agent.llm.constrained, **agent.sched_cfg.flags()})

    routes = [
        Route("/v1/chat", chat, methods=["POST"]),
        Route("/v1/confirmations/{request_id}", confirmation, methods=["POST"]),
        Route("/v1/events", events, methods=["GET"]),
        Route("/v1/memory", memories, methods=["GET"]),
        Route("/v1/memory/{memory_id}", memory_item, methods=["PUT", "DELETE"]),
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/stats", stats, methods=["GET"]),
        Route("/v1/runtime", runtime, methods=["GET", "POST"]),
    ]
    return Starlette(routes=routes, middleware=[Middleware(AuthMiddleware, auth=auth)])
