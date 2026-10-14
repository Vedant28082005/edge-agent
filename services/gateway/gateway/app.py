"""Public listener (127.0.0.1:8000, HTTPS terminated by `tailscale serve`). Implements gateway.yaml.

Role checks live here; device-hub applies none. Request bodies are never logged."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Annotated, Literal, Union

import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import policy
from .auth import AuthError, User
from .confirmations import ConfirmError
from .context import Ctx
from .media import AUDIO_TYPES, sniff_audio, sniff_image
from .events import Event
from .security import SECRETS, hash_password, new_id, sha256_hex
from .upstream import UpstreamError

log = logging.getLogger("gateway.api")

Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]
EP_RE = re.compile(r"^ep_[0-9a-f]{8}$")
NODE_RE = re.compile(r"^esp32-[0-9a-f]{12}$")
PLACEHOLDER = Path(__file__).resolve().parent.parent / "web_placeholder"


class SpaStatic(StaticFiles):
    """Static files for GET/HEAD only; any other method on an unknown or static path is a plain 404."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
            await jerr(404, "not_found")(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", headers: dict | None = None) -> None:
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def jerr(status: int, code: str, message: str = "", headers: dict | None = None) -> JSONResponse:
    body = {"error": code}
    if message:
        body["message"] = message
    return JSONResponse(body, status_code=status, headers=headers)


# ------------------------------------------------------------------ request models (strict, bounded)
class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(max_length=256)


class SetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(max_length=64)
    value: Scalar
    confirmation_token: str | None = Field(default=None, max_length=256)


EndpointType = Literal["light", "fan", "outlet", "lock", "cover", "thermostat", "sensor", "generic"]


class SceneTargetFixed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: str = Field(pattern=EP_RE.pattern)


class SceneTargetSelector(BaseModel):
    """Selector resolved at run time; fields combine with AND. At least one field, no unknown keys."""
    model_config = ConfigDict(extra="forbid")
    room: str | None = Field(default=None, max_length=48)
    type: EndpointType | None = None
    tag: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,24}$")

    @model_validator(mode="after")
    def _non_empty(self):
        if self.room is None and self.type is None and self.tag is None:
            raise ValueError("empty selector")
        return self


class SceneActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Union[SceneTargetFixed, SceneTargetSelector]
    capability: str = Field(max_length=64)
    value: Scalar


class SceneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=48)
    actions: list[SceneActionIn] = Field(min_length=1, max_length=50)

    def action_dicts(self) -> list[dict]:
        return [a.model_dump(exclude_none=True) for a in self.actions]


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str | None = Field(default=None, max_length=256)


class ConfirmSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["set_state"]
    endpoint_id: str = Field(pattern=EP_RE.pattern)
    capability: str = Field(max_length=64)
    value: Scalar


class ConfirmScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["run_scene"]
    scene: str = Field(min_length=1, max_length=64)


class MintIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str | None = Field(default=None, max_length=64)
    action: Union[ConfirmSet, ConfirmScene, None] = None


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    text: str | None = Field(default=None, max_length=4000)
    audio_ref: str | None = Field(default=None, max_length=64)
    image_refs: list[str] | None = Field(default=None, max_length=4)


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[Annotated[str, Field(max_length=64)]] = Field(max_length=64)


class WifiProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(max_length=64)


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)


class WifiIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ssid: str = Field(max_length=32)
    password: str = Field(max_length=64)


class ProvisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(pattern=NODE_RE.pattern)
    wifi_profile: str | None = Field(default=None, max_length=64)
    wifi: WifiIn | None = None


class IdentifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_s: int = Field(default=10, ge=1, le=60)


class EndpointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=48)
    room: str | None = Field(default=None, max_length=48)
    aliases: list[Annotated[str, Field(max_length=48)]] | None = Field(default=None, max_length=8)
    tags: list[Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,24}$")]] | None = Field(default=None, max_length=8)
    risk: Literal["normal", "confirm"] | None = None
    enabled: bool | None = None


class RebindIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(pattern=NODE_RE.pattern)
    local_id: str = Field(max_length=64)


class ApproveEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_id: str = Field(max_length=64)
    name: str = Field(max_length=48)
    room: str = Field(max_length=48)
    aliases: list[Annotated[str, Field(max_length=48)]] | None = Field(default=None, max_length=8)
    tags: list[Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,24}$")]] | None = Field(default=None, max_length=8)
    risk: Literal["normal", "confirm"] | None = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoints: list[ApproveEndpoint] = Field(max_length=64)


Role = Literal["admin", "member", "guest"]
RoomList = Annotated[list[Annotated[str, Field(min_length=1, max_length=48)]], Field(max_length=64)]


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: Role
    rooms: RoomList | None = None


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None = None
    rooms: RoomList | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
    disabled: bool | None = None


def dump(m: BaseModel) -> dict:
    return m.model_dump(exclude_none=True)


# ------------------------------------------------------------------ app
def create_public_app(ctx: Ctx) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if ctx.autostart:                              # hub/agent subscribers run inside the server's event loop
            ctx.start()
        yield
        if ctx.autostart:
            await ctx.shutdown()

    app = FastAPI(title="Edge Agent Gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    r = APIRouter()
    cfg = ctx.cfg
    # Uploads must never touch the disk: keep multipart parts in memory (Starlette spools >1 MiB to a temp file by default).
    from starlette.formparsers import MultiPartParser
    MultiPartParser.spool_max_size = max(MultiPartParser.spool_max_size, cfg.max_audio_bytes + cfg.max_image_bytes + (1 << 20))
    upload_caps = {"/images": cfg.max_image_bytes, "/voice": cfg.max_audio_bytes}

    # ---- errors: one shape, and never echo request input back
    @app.exception_handler(ApiError)
    async def _api_err(_: Request, e: ApiError):
        return jerr(e.status, e.code, e.message, e.headers)

    @app.exception_handler(ConfirmError)
    async def _conf_err(_: Request, e: ConfirmError):
        return jerr(e.status, e.code, e.message)

    @app.exception_handler(UpstreamError)
    async def _up_err(_: Request, e: UpstreamError):
        code = "vision_unavailable" if e.service == "vision-node" and e.status != 504 else             ("upstream_timeout" if e.status == 504 else "upstream_error")
        return jerr(504 if e.status == 504 else 502, code, f"{e.service} unavailable")

    @app.exception_handler(RequestValidationError)
    async def _val_err(_: Request, e: RequestValidationError):
        loc = ".".join(str(x) for x in (e.errors()[0].get("loc", ())[1:] if e.errors() else ()))
        return jerr(400, "bad_request", f"invalid field: {loc}" if loc else "invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def _http_err(_: Request, e: StarletteHTTPException):
        return jerr(e.status_code, {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "error"))

    @app.middleware("http")
    async def _no_internal(request: Request, call_next):
        # The public listener never serves /internal/* (any method): 404 before routing and before the static mount.
        if request.url.path == "/internal" or request.url.path.startswith("/internal/"):
            return jerr(404, "not_found")
        cap = upload_caps.get(request.url.path)
        if cap is not None and request.method == "POST":       # refuse oversized bodies before parsing (and chunked ones)
            cl = request.headers.get("content-length", "")
            if not cl.isdigit() or int(cl) > cap + 65536:        # chunked uploads are refused too: the size must be known up front
                return jerr(413, "too_large", f"max {cap} bytes")
        elif request.method in ("POST", "PUT", "PATCH") and request.headers.get("content-length", "0").isdigit()                 and int(request.headers.get("content-length", "0")) > 262144:
            return jerr(413, "too_large", "request body too large")
        resp = await call_next(request)
        if request.url.path.startswith(("/auth", "/admin", "/confirmations")):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ---- dependencies
    def authed(request: Request) -> User:
        h = request.headers.get("authorization", "")
        if h[:7].lower() != "bearer ":
            raise ApiError(401, "unauthorized", "missing or invalid token")
        u = ctx.auth.authenticate_access(h[7:].strip())
        if u is None:
            raise ApiError(401, "unauthorized", "missing or invalid token")
        return u

    def admin(u: User = Depends(authed)) -> User:
        if not u.is_admin:
            raise ApiError(403, "forbidden", "admin role required")
        return u

    async def hub(method: str, path: str, u: User | None = None, *, ok=(200,), **kw):
        status, body = await ctx.hub.hub(method, path, actor=u.user_id if u else None, **kw)
        if status in (401, 403):                       # our own service credential is wrong: not the user's problem
            log.error("device-hub rejected the gateway service secret (HTTP %s)", status)
            raise ApiError(502, "upstream_error", "device-hub rejected the gateway credential")
        if status >= 500:
            raise ApiError(504 if status == 504 else 502, "upstream_timeout" if status == 504 else "upstream_error",
                           "device-hub error")
        return status, body

    def limit(name: str, u: User) -> None:
        ra = ctx.limiter.hit(name, u.user_id)
        if ra:
            raise ApiError(429, "rate_limited", "too many requests", {"Retry-After": str(ra)})

    def fwd(status: int, body) -> Response:
        if body is None:
            return Response(status_code=status)
        return JSONResponse(body, status_code=status)

    # ================================================================== auth
    @r.post("/auth/login")
    async def login(body: LoginIn, request: Request):
        ip = request.client.host if request.client else "?"
        try:
            return await ctx.auth.login(body.username, body.password, ip)
        except AuthError as e:
            raise ApiError(e.status, e.code, headers={"Retry-After": str(e.retry_after)} if e.retry_after else None)

    @r.post("/auth/refresh")
    async def refresh(body: RefreshIn):
        try:
            return ctx.auth.refresh(body.refresh_token)
        except AuthError as e:
            raise ApiError(e.status, e.code)

    # ================================================================== devices
    @r.get("/devices")
    async def list_devices(room: str | None = None, type: str | None = None, u: User = Depends(authed)):
        params = {k: v for k, v in (("room", room), ("type", type)) if v}
        status, body = await hub("GET", "/devices", u, params=params)
        if status != 200 or not isinstance(body, list):
            return fwd(status, body)
        return [e for e in body if policy.endpoint_visible(u, e)]

    @r.post("/devices/{endpoint_id}/set")
    async def set_device(endpoint_id: str, body: SetIn, u: User = Depends(authed)):
        if u.role == "guest":
            raise ApiError(403, "forbidden", "guests run assigned scenes only")
        if not EP_RE.match(endpoint_id):
            raise ApiError(404, "not_found")
        status, ep = await hub("GET", f"/devices/{endpoint_id}", u)
        if status == 404:
            raise ApiError(404, "not_found")
        if status != 200 or not isinstance(ep, dict):
            return fwd(status, ep)
        if not policy.endpoint_visible(u, ep):
            raise ApiError(403, "forbidden", "no access to this room")
        if body.confirmation_token:
            SECRETS.add(body.confirmation_token)
        status, res = await hub("POST", f"/devices/{endpoint_id}/set", u, json={
            "capability": body.capability, "value": body.value, "user_id": u.user_id,
            **({"confirmation_token": body.confirmation_token} if body.confirmation_token else {})})
        if status == 404:
            raise ApiError(404, "not_found")
        if status != 200 or not isinstance(res, dict):
            return fwd(status, res)
        ctx.db.audit(u.user_id, "device.set", endpoint_id, {"capability": body.capability, "ok": bool(res.get("ok")),
                                                            "error": res.get("error")})
        if not res.get("ok") and res.get("error") in ("confirmation_required", "offline"):
            code = "endpoint_offline" if res["error"] == "offline" else "confirmation_required"
            return jerr(409, code, "nothing was executed")
        return res

    # ================================================================== scenes
    async def room_map() -> dict[str, str]:
        status, body = await hub("GET", "/devices")
        return {e["endpoint_id"]: e.get("room", "") for e in body} if status == 200 and isinstance(body, list) else {}

    async def check_scene_input(u: User, actions: list[dict]) -> None:
        if u.is_admin:
            return
        rooms = policy.action_rooms(actions, await room_map())
        if "*" in rooms or not rooms <= u.rooms:
            raise ApiError(403, "forbidden", "members may only use rooms they can access (no room-less selectors)")

    @r.get("/scenes")
    async def list_scenes(u: User = Depends(authed)):
        status, body = await hub("GET", "/scenes", u)
        if status != 200 or not isinstance(body, list):
            return fwd(status, body)
        assigned = ctx.confirm.assigned(u)
        return [s for s in body if policy.scene_visible(u, s, assigned)]

    @r.post("/scenes")
    async def create_scene(body: SceneIn, u: User = Depends(authed)):
        if u.role == "guest":
            raise ApiError(403, "forbidden")
        acts = body.action_dicts()
        await check_scene_input(u, acts)
        status, res = await hub("POST", "/scenes", u, json={"name": body.name, "actions": acts, "owner": u.user_id})
        if status == 201:
            ctx.db.audit(u.user_id, "scene.create", res.get("scene_id", "") if isinstance(res, dict) else "")
        return fwd(status, res)

    async def own_or_admin(u: User, scene_id: str) -> dict:
        if u.role == "guest":
            raise ApiError(403, "forbidden")
        status, scene = await hub("GET", f"/scenes/{scene_id}", u)
        if status == 404:
            raise ApiError(404, "not_found")
        if status != 200 or not isinstance(scene, dict):
            raise ApiError(502, "hub_error")
        if not u.is_admin and scene.get("owner") != u.user_id:
            raise ApiError(403, "forbidden", "members may change their own scenes only")
        return scene

    @r.put("/scenes/{scene_id}")
    async def update_scene(scene_id: str, body: SceneIn, u: User = Depends(authed)):
        await own_or_admin(u, scene_id)
        acts = body.action_dicts()
        await check_scene_input(u, acts)
        status, res = await hub("PUT", f"/scenes/{scene_id}", u, json={"name": body.name, "actions": acts})
        if status == 200:
            ctx.db.audit(u.user_id, "scene.update", scene_id)
        return fwd(status, res)

    @r.delete("/scenes/{scene_id}")
    async def delete_scene(scene_id: str, u: User = Depends(authed)):
        await own_or_admin(u, scene_id)
        status, res = await hub("DELETE", f"/scenes/{scene_id}", u)
        if status == 204:
            ctx.db.run("DELETE FROM scene_assignments WHERE scene_id=?", (scene_id,))
            ctx.db.audit(u.user_id, "scene.delete", scene_id)
        return fwd(status, res)

    @r.post("/scenes/{scene_id}/run")
    async def run_scene(scene_id: str, body: RunIn | None = None, u: User = Depends(authed)):
        status, scene = await hub("GET", f"/scenes/{scene_id}", u)
        if status == 404:
            raise ApiError(404, "not_found")
        if status != 200 or not isinstance(scene, dict):
            raise ApiError(502, "hub_error")
        if not policy.can_run_scene(u, scene, ctx.confirm.assigned(u)):
            raise ApiError(403, "forbidden")
        tok = body.confirmation_token if body and u.role != "guest" else None     # guests never carry a token
        if tok:
            SECRETS.add(tok)
        status, res = await hub("POST", f"/scenes/{scene['scene_id']}/run", u, json={
            "user_id": u.user_id, **({"confirmation_token": tok} if tok else {})})
        ctx.db.audit(u.user_id, "scene.run", scene["scene_id"], {"status": status})
        return fwd(status, res)

    # ================================================================== confirmations
    @r.post("/confirmations")
    async def mint(body: MintIn, u: User = Depends(authed)):
        limit("confirm", u)
        if (body.request_id is None) == (body.action is None):
            raise ApiError(400, "bad_request", "send exactly one of request_id or action")
        return await ctx.confirm.approve(u, body.request_id, dump(body.action) if body.action else None)

    @r.post("/confirmations/{request_id}/decline")
    async def decline(request_id: str, u: User = Depends(authed)):
        await ctx.confirm.decline(u, request_id)
        return Response(status_code=204)

    @r.post("/confirmations/{request_id}/retry", status_code=202)
    async def retry(request_id: str, u: User = Depends(authed)):
        limit("confirm", u)
        return {"request_id": await ctx.confirm.retry(u, request_id)}

    # ================================================================== chat / voice / images / memory
    async def agent_json(method: str, path: str, **kw):
        status, body = await ctx.agent.call(method, path, **kw)
        if status in (401, 403) or status >= 500:
            raise ApiError(504 if status == 504 else 502, "upstream_timeout" if status == 504 else "upstream_error", "agent error")
        return status, body

    @r.post("/chat")
    async def chat(body: ChatIn, u: User = Depends(authed)):
        limit("chat", u)
        if not (body.text or body.audio_ref or body.image_refs):
            raise ApiError(400, "bad_request", "text, audio_ref or image_refs required")
        refs = [(body.audio_ref, "voice")] if body.audio_ref else []
        refs += [(x, "image") for x in body.image_refs or []]
        if not all(ctx.media.has(ref, u.user_id, kind) for ref, kind in refs) or len({x for x, _ in refs}) != len(refs):
            raise ApiError(400, "bad_media_ref", "unknown, expired, used or foreign media reference")
        text = body.text
        if body.audio_ref:                                   # voice is a transcript by the time it reaches the agent
            a = ctx.media.take(body.audio_ref, u.user_id, "voice")
            text = ((text + "\n") if text else "") + (a.text or "")
            text = text[:4000]
        images = []
        for ref in body.image_refs or []:
            it = ctx.media.take(ref, u.user_id, "image")     # single use: gone from memory after this call
            images.append({"ref": ref, "media_type": it.content_type, "data_b64": base64.b64encode(it.data).decode()})
        client_sid = body.session_id or new_id("s_")
        # agent-internal.yaml ChatRequest: exactly these fields; session ids are per user and at most 64 chars
        payload = {"user_id": u.user_id, "role": u.role, "session_id": "s_" + sha256_hex(f"{u.user_id}\0{client_sid}")[:48],
                   **({"text": text} if text else {}), **({"images": images} if images else {})}
        cm = ctx.agent.stream("POST", "/v1/chat", json=payload, headers={"Accept": "text/event-stream"})
        try:
            resp = await cm.__aenter__()
        except httpx.TimeoutException:
            raise UpstreamError("agent", 504, "upstream_timeout") from None
        except httpx.HTTPError:
            raise UpstreamError("agent") from None
        if resp.status_code != 200:
            data = await resp.aread()
            await cm.__aexit__(None, None, None)
            try:
                err = json.loads(data)
            except ValueError:
                err = {}
            if resp.status_code == 429:                       # agent busy / budget exceeded
                raise ApiError(429, "rate_limited", "the agent is busy", {"Retry-After": resp.headers.get("retry-after", "5")})
            if resp.status_code == 400:
                return fwd(400, {"error": "bad_request", "message": str((err or {}).get("message", ""))[:200]})
            raise ApiError(504 if resp.status_code == 504 else 502, "upstream_timeout" if resp.status_code == 504 else "upstream_error",
                           "agent error")

        async def gen():
            frame: list[str] = []
            try:
                async for line in resp.aiter_lines():
                    yield (line + "\n").encode()
                    if line:
                        frame.append(line)
                        continue
                    inspect(frame, u, client_sid)
                    frame = []
            except Exception as e:                                  # mid-stream failure: end the stream
                log.warning("chat stream ended early: %s", type(e).__name__)
            finally:
                await cm.__aexit__(None, None, None)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-Id": client_sid})

    def inspect(frame: list[str], u: User, sid: str) -> None:
        """Look at forwarded SSE frames: remember confirmation requests (and show them on the user's other sockets)."""
        ev, data = None, []
        for ln in frame:
            if ln.startswith("event:"):
                ev = ln[6:].strip()
            elif ln.startswith("data:"):
                data.append(ln[5:].lstrip(" "))
        if ev != "confirmation_request" or not data:
            return
        try:
            d = json.loads("\n".join(data))
        except ValueError:
            return
        if isinstance(d, dict) and ctx.confirm.register_request(u.user_id, sid, d):
            ctx.bc.publish(Event("confirmation_request", {k: d.get(k) for k in
                                 ("request_id", "summary", "action", "action_hash", "expires_in_s")}, user_id=u.user_id))

    async def read_upload(f: UploadFile, limit_: int) -> bytes:
        data = await f.read(limit_ + 1)
        if len(data) > limit_:
            raise ApiError(413, "too_large", f"max {limit_} bytes")
        if not data:
            raise ApiError(400, "bad_request", "empty upload")
        return data

    @r.post("/voice")
    async def voice(audio: UploadFile = File(...), u: User = Depends(authed)):
        limit("voice", u)
        data = await read_upload(audio, cfg.max_audio_bytes)
        if not sniff_audio(data):                            # the container is sniffed; the Content-Type is not trusted
            raise ApiError(415, "unsupported_media_type", "wav, webm, m4a or aac only")
        status, res = await ctx.vision.call("POST", "/transcribe", files={"audio": (audio.filename or "audio", data, "application/octet-stream")})
        del data
        if status != 200 or not isinstance(res, dict):
            raise ApiError(502, "vision_unavailable", "vision-node could not transcribe")
        text = str(res.get("text") or "")[:4000]
        ref = ctx.media.put(u.user_id, "voice", None, "text/plain", text)  # transcript only; the audio is already dropped
        if ref is None:
            raise ApiError(429, "too_many_uploads", "too many held uploads", {"Retry-After": str(cfg.media_ttl_s)})
        return {"audio_ref": ref, "text": text}

    @r.post("/images")
    async def images(image: UploadFile = File(...), u: User = Depends(authed)):
        data = await read_upload(image, cfg.max_image_bytes)
        ct = sniff_image(data)
        if ct is None:
            raise ApiError(415, "unsupported_media_type", "jpeg, png or webp only")
        ref = ctx.media.put(u.user_id, "image", data, ct)                 # memory only, short TTL, removed after use
        if ref is None:
            raise ApiError(429, "too_many_uploads", "too many held uploads", {"Retry-After": str(cfg.media_ttl_s)})
        return {"image_ref": ref}

    @r.get("/memory")
    async def list_memory(u: User = Depends(authed)):
        if u.role == "guest":
            return []
        status, body = await agent_json("GET", "/v1/memory", params={"user_id": u.user_id})
        return fwd(status, body) if status != 200 else body

    @r.put("/memory/{memory_id}")
    async def put_memory(memory_id: str, body: MemoryIn, u: User = Depends(authed)):
        if u.role == "guest":
            raise ApiError(403, "forbidden")
        status, res = await agent_json("PUT", f"/v1/memory/{memory_id}", json={"user_id": u.user_id, "text": body.text})
        return fwd(status, res) if res is not None else Response(status_code=status)

    @r.delete("/memory/{memory_id}")
    async def del_memory(memory_id: str, u: User = Depends(authed)):
        if u.role == "guest":
            raise ApiError(403, "forbidden")
        status, res = await agent_json("DELETE", f"/v1/memory/{memory_id}", params={"user_id": u.user_id})
        return Response(status_code=status) if status == 204 else fwd(status, res)

    # ================================================================== system
    @r.get("/system/health")
    async def health(u: User = Depends(authed)):
        async def probe(name: str, client, path="/health") -> dict:
            try:
                status, b = await asyncio.wait_for(client.call("GET", path, timeout=2.0), 2.5)
            except (UpstreamError, asyncio.TimeoutError):
                return {"name": name, "up": False}
            row = {"name": name, "up": status == 200}
            if isinstance(b, dict):
                for k in ("mem_available_mb", "temp_c", "queue_depth"):
                    if isinstance(b.get(k), (int, float)) and not isinstance(b.get(k), bool):
                        row[k] = int(b[k]) if k != "temp_c" else b[k]
                if name == "device-hub" and isinstance(b.get("pending_acks"), int):
                    row["queue_depth"] = b["pending_acks"]
            return row
        rows = await asyncio.gather(probe("device-hub", ctx.hub), probe("agent", ctx.agent, "/v1/health"),
                                    probe("vision-node", ctx.vision), probe("yolo-node", ctx.yolo))
        return {"nodes": [{"name": "gateway", "up": True}, *rows]}

    @r.post("/ws/ticket")
    async def ws_ticket(request: Request, u: User = Depends(authed)):
        limit("ticket", u)
        claims = ctx.auth.jwt.decode(request.headers["authorization"][7:].strip())
        t = ctx.tickets.mint(u.user_id, claims["exp"])
        if t is None:
            raise ApiError(429, "rate_limited", "too many unused tickets", {"Retry-After": str(cfg.ticket_ttl_s)})
        return {"ticket": t, "expires_in_s": cfg.ticket_ttl_s}

    TICKET_PROTO = "edge-ticket."

    @r.websocket("/ws")
    async def ws(sock: WebSocket):
        """Auth: a single-use ticket from POST /ws/ticket in `Sec-WebSocket-Protocol: edge-ticket.<ticket>` or in a first
        `{"type":"auth","ticket":...}` message (5 s), or an `Authorization: Bearer` header. NEVER a token in the URL."""
        proto = next((p.strip() for p in sock.headers.get("sec-websocket-protocol", "").split(",")
                      if p.strip().startswith(TICKET_PROTO)), None)
        ident = None                                              # (user_id, access token exp)
        h = sock.headers.get("authorization", "")
        if h[:7].lower() == "bearer ":
            u0 = ctx.auth.authenticate_access(h[7:].strip())
            c0 = ctx.auth.jwt.decode(h[7:].strip()) if u0 else None
            ident = (u0.user_id, c0["exp"]) if c0 else None
        elif proto:
            ident = ctx.tickets.take(proto[len(TICKET_PROTO):])
        await sock.accept(subprotocol=proto if (proto and ident) else None)
        if ident is None and not proto and not h:
            try:
                first = json.loads(await asyncio.wait_for(sock.receive_text(), cfg.ws_auth_timeout_s))
                if isinstance(first, dict) and first.get("type") == "auth" and isinstance(first.get("ticket"), str):
                    ident = ctx.tickets.take(first["ticket"])
            except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, RuntimeError):
                pass
        user = ctx.auth.get(ident[0]) if ident else None          # role and rooms are re-read from the DB now
        if user is None or user.disabled:
            await sock.close(code=4401)
            return
        if ctx.limiter.hit("ws", user.user_id):
            await sock.close(code=4429)
            return
        client = ctx.bc.add(user)
        await sock.send_json({"type": "notification", "level": "info", "text": "connected", "role": user.role})

        async def pump():
            while not client.closed.is_set():
                getter = asyncio.ensure_future(client.queue.get())
                closer = asyncio.ensure_future(client.closed.wait())
                done, _ = await asyncio.wait({getter, closer}, return_when=asyncio.FIRST_COMPLETED)
                for t in (getter, closer):
                    if t not in done:
                        t.cancel()
                if getter in done:
                    await sock.send_json(getter.result())

        flooded = asyncio.Event()

        async def reader():
            while True:
                await sock.receive_text()                       # client messages are ignored; this notices disconnects
                if ctx.limiter.hit("ws_msg", user.user_id):     # inbound message flood: close 4429, the client backs off
                    flooded.set()
                    return

        expiry = max(1.0, ident[1] - ctx.clock())               # the socket ends with the access token that created it
        tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(reader())]
        try:
            await asyncio.wait(tasks, timeout=expiry, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            ctx.bc.remove(client)
            try:
                await sock.close(code=4429 if flooded.is_set() else 4401)
            except (RuntimeError, WebSocketDisconnect):
                pass                                            # the peer is already gone

    # ================================================================== /admin/*  (admin only)
    def log_admin(u: User, action: str, target: str = "", detail: dict | None = None, result: str = "ok") -> None:
        ctx.db.audit(u.user_id, f"admin.{action}", target, detail, result)

    @r.post("/admin/provision-sessions", status_code=201)
    async def prov(body: ProvisionIn, u: User = Depends(admin)):
        if body.wifi is not None:
            wifi = {"ssid": body.wifi.ssid, "password": body.wifi.password}
        elif body.wifi_profile:
            wifi = ctx.profiles.resolve(body.wifi_profile)
            if wifi is None:
                raise ApiError(404, "unknown_wifi_profile")
        else:
            raise ApiError(400, "bad_request", "wifi or wifi_profile required")
        SECRETS.add(wifi["password"])
        status, res = await hub("POST", "/admin/provision-sessions", u, json={"node_id": body.node_id, "wifi": wifi})
        sid = ""
        if status == 201 and isinstance(res, dict):
            sid = str(res.get("session_id", ""))
            m = (res.get("bundle") or {}).get("mqtt") or {}
            SECRETS.add(m.get("password"))
        log_admin(u, "provision_session", body.node_id, {"session_id": sid, "wifi_profile": body.wifi_profile,
                                                         "status": status}, "ok" if status == 201 else "error")
        resp = fwd(status, res)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @r.get("/admin/nodes")
    async def nodes(u: User = Depends(admin)):
        return fwd(*await hub("GET", "/admin/nodes", u))

    def node_path(node_id: str) -> str:
        if not NODE_RE.match(node_id):
            raise ApiError(404, "not_found")
        return f"/admin/nodes/{node_id}"

    @r.post("/admin/nodes/{node_id}/approve")
    async def approve(node_id: str, body: ApproveIn, u: User = Depends(admin)):
        status, res = await hub("POST", node_path(node_id) + "/approve", u, json=dump(body))
        log_admin(u, "approve_node", node_id, {"status": status, "n": len(body.endpoints)})
        return fwd(status, res)

    @r.post("/admin/nodes/{node_id}/identify", status_code=202)
    async def identify(node_id: str, body: IdentifyIn | None = None, u: User = Depends(admin)):
        status, res = await hub("POST", node_path(node_id) + "/identify", u, json=dump(body or IdentifyIn()))
        log_admin(u, "identify_node", node_id, {"status": status})
        return fwd(status, res)

    @r.post("/admin/nodes/{node_id}/unprovision", status_code=202)
    async def unprovision(node_id: str, u: User = Depends(admin)):
        status, res = await hub("POST", node_path(node_id) + "/unprovision", u)
        log_admin(u, "unprovision_node", node_id, {"status": status})
        return fwd(status, res)

    @r.delete("/admin/nodes/{node_id}")
    async def forget(node_id: str, confirm: bool = False, u: User = Depends(admin)):
        status, res = await hub("DELETE", node_path(node_id), u, params={"confirm": "true" if confirm else "false"})
        log_admin(u, "forget_node", node_id, {"status": status, "confirm": confirm})
        return fwd(status, res)

    def ep_path(endpoint_id: str) -> str:
        if not EP_RE.match(endpoint_id):
            raise ApiError(404, "not_found")
        return f"/admin/endpoints/{endpoint_id}"

    @r.patch("/admin/endpoints/{endpoint_id}")
    async def patch_ep(endpoint_id: str, body: EndpointPatch, u: User = Depends(admin)):
        data = dump(body)
        if not data:
            raise ApiError(400, "bad_request", "at least one field required")
        status, res = await hub("PATCH", ep_path(endpoint_id), u, json=data)
        log_admin(u, "patch_endpoint", endpoint_id, {"fields": sorted(data), "status": status})
        return fwd(status, res)

    @r.post("/admin/endpoints/{endpoint_id}/rebind")
    async def rebind(endpoint_id: str, body: RebindIn, u: User = Depends(admin)):
        status, res = await hub("POST", ep_path(endpoint_id) + "/rebind", u, json=dump(body))
        log_admin(u, "rebind_endpoint", endpoint_id, {"to": body.node_id, "status": status})
        return fwd(status, res)

    # ---- users
    @r.get("/admin/users")
    async def users(u: User = Depends(admin)):
        return [x.public() for x in ctx.auth.list()]

    @r.post("/admin/users", status_code=201)
    async def create_user(body: UserCreate, u: User = Depends(admin)):
        pw = await asyncio.to_thread(hash_password, body.password)
        try:
            nu = ctx.auth.create(body.username, pw, body.role, body.rooms)
        except Exception:
            raise ApiError(409, "username_taken") from None
        log_admin(u, "user_create", nu.user_id, {"role": nu.role})
        return JSONResponse(nu.public(), status_code=201)

    def lockout_guard(target_id: str, new_role: str | None, disabling: bool, deleting: bool) -> None:
        t = ctx.auth.get(target_id)
        if t and t.is_admin and not t.disabled and (deleting or disabling or (new_role and new_role != "admin")) \
                and ctx.auth.admin_count() <= 1:
            raise ApiError(409, "last_admin", "cannot remove or demote the last active admin")

    @r.patch("/admin/users/{user_id}")
    async def patch_user(user_id: str, body: UserPatch, u: User = Depends(admin)):
        data = dump(body)
        if not data:
            raise ApiError(400, "bad_request", "at least one field required")
        lockout_guard(user_id, body.role, bool(body.disabled), False)
        pw = await asyncio.to_thread(hash_password, body.password) if body.password else None
        nu = ctx.auth.patch(user_id, role=body.role, rooms=body.rooms, password_hash=pw, disabled=body.disabled)
        if nu is None:
            raise ApiError(404, "not_found")
        ctx.bc.update_user(user_id, nu)
        log_admin(u, "user_patch", user_id, {"fields": sorted(k for k in data if k != "password") + (["password"] if pw else [])})
        return nu.public()

    @r.delete("/admin/users/{user_id}")
    async def delete_user(user_id: str, u: User = Depends(admin)):
        lockout_guard(user_id, None, False, True)
        if not ctx.auth.delete(user_id):
            raise ApiError(404, "not_found")
        ctx.bc.update_user(user_id, None)
        log_admin(u, "user_delete", user_id)
        return Response(status_code=204)

    # ---- scene assignments (guests run assigned scenes only)
    async def scene_exists(scene_id: str) -> None:
        status, _ = await hub("GET", f"/scenes/{scene_id}")
        if status == 404:
            raise ApiError(404, "not_found")
        if status != 200:
            raise ApiError(502, "upstream_error")

    def assignments(scene_id: str) -> dict:
        return {"user_ids": [x["user_id"] for x in ctx.db.all(
            "SELECT user_id FROM scene_assignments WHERE scene_id=? ORDER BY user_id", (scene_id,))]}

    @r.get("/admin/scenes/{scene_id}/assignments")
    async def get_assignments(scene_id: str, u: User = Depends(admin)):
        await scene_exists(scene_id)
        return assignments(scene_id)

    @r.put("/admin/scenes/{scene_id}/assignments")
    async def set_assignments(scene_id: str, body: AssignIn, u: User = Depends(admin)):
        await scene_exists(scene_id)
        ids = list(dict.fromkeys(body.user_ids))
        if len(ids) != len(body.user_ids):
            raise ApiError(400, "bad_request", "duplicate user ids")
        for uid in ids:
            who = ctx.auth.get(uid)
            if who is None or who.role != "guest":
                raise ApiError(400, "bad_request", "unknown user or user is not a guest")
        with ctx.db.tx():
            ctx.db.run("DELETE FROM scene_assignments WHERE scene_id=?", (scene_id,))
            for uid in ids:
                ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (uid, scene_id))
        log_admin(u, "scene_assign", scene_id, {"users": len(ids)})
        return assignments(scene_id)

    # ---- Wi-Fi profiles: the password is write-only (stored encrypted; never returned, logged or audited)
    @r.get("/admin/wifi-profiles")
    async def list_wifi(u: User = Depends(admin)):
        return ctx.profiles.list()

    @r.post("/admin/wifi-profiles", status_code=201)
    async def put_wifi(body: WifiProfileIn, u: User = Depends(admin)):
        SECRETS.add(body.password)
        ctx.profiles.put(body.name, body.ssid, body.password)
        log_admin(u, "wifi_profile_put", body.name)
        return JSONResponse({"name": body.name, "ssid": body.ssid}, status_code=201)

    @r.delete("/admin/wifi-profiles/{name}")
    async def del_wifi(name: str, u: User = Depends(admin)):
        if not ctx.profiles.delete(name):
            raise ApiError(404, "not_found")
        log_admin(u, "wifi_profile_delete", name)
        return Response(status_code=204)

    app.include_router(r)
    static = Path(cfg.static_dir) if cfg.static_dir and Path(cfg.static_dir).is_dir() else PLACEHOLDER
    if static.is_dir():
        app.mount("/", SpaStatic(directory=str(static), html=True), name="web")
    app.state.ctx = ctx
    return app
