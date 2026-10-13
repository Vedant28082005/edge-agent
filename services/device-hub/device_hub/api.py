"""Internal HTTP API on 127.0.0.1:8100 for the gateway (admin + scene + device operations), the registry-change
event stream (SSE) and the MCP endpoint (/mcp, streamable HTTP). Specified in INTERNAL_API.md (a proposal: the
contracts do not define it).

Errors are {"error": code, "message": text}. Request bodies are size-limited. Secrets (confirmation tokens, Wi-Fi
password) only appear in request/response bodies, never in URLs, and are never logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .errors import HubError
from .events import ALL_TYPES, DEFAULT_TYPES
from .hub import Hub
from .mcp_server import build_mcp_server
from .util import SECRETS, action_hash, sanitize_text, set_state_action

log = logging.getLogger("device_hub.api")

MAX_BODY = 64 * 1024
AGENT_ALLOWED = ("/mcp", "/events", "/registry/version", "/health")


def _err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _body(request: Request, required: bool = True) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HubError("too_large", "request body too large", 413)
    if not raw.strip():
        if required:
            raise HubError("bad_request", "JSON body required")
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        raise HubError("bad_request", "body is not valid JSON") from None
    if not isinstance(doc, dict):
        raise HubError("bad_request", "body must be a JSON object")
    return doc


def _actor(request: Request) -> str:
    return sanitize_text(request.headers.get("x-actor", "gateway"), 64) or "gateway"


def _bool(request: Request, name: str) -> bool:
    return request.query_params.get(name, "").lower() in ("1", "true", "yes")


def handler(fn: Callable[..., Any], body: str | None = None):
    """Wrap a sync function(hub, request, parsed_body) into a Starlette endpoint with uniform error handling.
    body: None = no body, "required" or "optional"."""

    async def endpoint(request: Request):
        hub: Hub = request.app.state.hub
        try:
            parsed = await _body(request, required=(body == "required")) if body else {}
            result = await run_in_threadpool(fn, hub, request, parsed)
        except HubError as e:
            return JSONResponse(e.to_dict(), status_code=e.status)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error in %s", request.url.path)
            return _err("internal_error", "internal error", 500)
        if isinstance(result, Response):
            return result
        if result is None:
            return Response(status_code=204)
        return JSONResponse(result)

    return endpoint


# ------------------------------------------------------------------------------------------------ admin routes
def create_provision_session(hub: Hub, r: Request, b: dict):
    return JSONResponse(hub.admin.create_provision_session(b, _actor(r)), status_code=201)


def list_sessions(hub: Hub, r: Request, b: dict):
    return hub.provisioning.list_sessions(active_only=_bool(r, "active"))


def list_nodes(hub: Hub, r: Request, b: dict):
    return hub.admin.list_nodes()


def get_node(hub: Hub, r: Request, b: dict):
    return hub.admin.get_node(r.path_params["node_id"])


def approve_node(hub: Hub, r: Request, b: dict):
    return hub.admin.approve_node(r.path_params["node_id"], b, _actor(r))


def identify_node(hub: Hub, r: Request, b: dict):
    res = hub.admin.identify_node(r.path_params["node_id"], b.get("duration_s"), _actor(r))
    return JSONResponse(res, status_code=202)


def unprovision_node(hub: Hub, r: Request, b: dict):
    return JSONResponse(hub.admin.unprovision_node(r.path_params["node_id"], _bool(r, "force"), _actor(r)), status_code=202)


def forget_node(hub: Hub, r: Request, b: dict):
    res = hub.admin.forget_node(r.path_params["node_id"], _bool(r, "confirm"), _bool(r, "force"), _actor(r))
    if res["forgotten"]:
        return Response(status_code=204)
    return JSONResponse({"error": "confirmation_required", **{k: res[k] for k in ("scenes", "automations", "scene_list", "message")}},
                        status_code=409)


def patch_endpoint(hub: Hub, r: Request, b: dict):
    return hub.admin.patch_endpoint(r.path_params["endpoint_id"], b, _actor(r))


def rebind_endpoint(hub: Hub, r: Request, b: dict):
    return hub.admin.rebind_endpoint(r.path_params["endpoint_id"], b, _actor(r))


def audit(hub: Hub, r: Request, b: dict):
    try:
        limit = int(r.query_params.get("limit", "100"))
    except ValueError:
        raise HubError("bad_request", "limit must be an integer") from None
    return hub.registry.list_audit(limit, r.query_params.get("action"))


def rejections(hub: Hub, r: Request, b: dict):
    return list(hub.bridge.rejections)


# ------------------------------------------------------------------------------------------------ device routes
def list_devices(hub: Hub, r: Request, b: dict):
    etype = r.query_params.get("type")
    if etype and etype not in hub.registry.endpoint_types():
        raise HubError("bad_request", "unknown endpoint type")
    return hub.registry.list_endpoints(r.query_params.get("room"), etype, include_disabled=True)


def get_device(hub: Hub, r: Request, b: dict):
    return hub.registry.get_endpoint(r.path_params["endpoint_id"])


def set_device(hub: Hub, r: Request, b: dict):
    unknown = set(b) - {"capability", "value", "user_id", "confirmation_token"}
    if unknown or "capability" not in b or "value" not in b:
        raise HubError("bad_request", "body must be {capability, value, user_id?, confirmation_token?}")
    hub.registry.get_endpoint(r.path_params["endpoint_id"])         # 404 for unknown
    tok = b.get("confirmation_token")
    if isinstance(tok, str):
        SECRETS.add(tok, permanent=False)
    return hub.executor.set_state(r.path_params["endpoint_id"], b["capability"], b["value"],
                                  user_id=b.get("user_id") if isinstance(b.get("user_id"), str) else None,
                                  token=tok if isinstance(tok, str) else None)


def set_action(hub: Hub, r: Request, b: dict):
    """The canonical action + hash the gateway needs to mint a token for a set_state on this endpoint."""
    ep = hub.registry.get_endpoint(r.path_params["endpoint_id"])
    if "capability" not in b or "value" not in b:
        raise HubError("bad_request", "body must be {capability, value}")
    action = set_state_action(ep["endpoint_id"], b["capability"], b["value"])
    return {"action": action, "action_hash": action_hash(action), "risk": ep["risk"]}


# ------------------------------------------------------------------------------------------------ scene routes
def list_scenes(hub: Hub, r: Request, b: dict):
    return hub.scenes.list_scenes()


def create_scene(hub: Hub, r: Request, b: dict):
    unknown = set(b) - {"name", "actions", "owner"}
    if unknown:
        raise HubError("bad_request", f"unknown fields: {sorted(unknown)}")
    return JSONResponse(hub.scenes.create_scene(b.get("name"), b.get("actions"), str(b.get("owner", "")), _actor(r)), status_code=201)


def get_scene(hub: Hub, r: Request, b: dict):
    return hub.scenes.get_scene(r.path_params["scene_id"])


def update_scene(hub: Hub, r: Request, b: dict):
    unknown = set(b) - {"name", "actions"}
    if unknown:
        raise HubError("bad_request", f"unknown fields: {sorted(unknown)}")
    return hub.scenes.update_scene(r.path_params["scene_id"], b.get("name"), b.get("actions"), _actor(r))


def delete_scene(hub: Hub, r: Request, b: dict):
    hub.scenes.delete_scene(r.path_params["scene_id"], _actor(r))
    return None


def run_scene(hub: Hub, r: Request, b: dict):
    unknown = set(b) - {"user_id", "confirmation_token"}
    if unknown:
        raise HubError("bad_request", "body must be {user_id?, confirmation_token?}")
    tok = b.get("confirmation_token")
    if isinstance(tok, str):
        SECRETS.add(tok, permanent=False)
    res = hub.scenes.run_scene(r.path_params["scene_id"], user_id=b.get("user_id") if isinstance(b.get("user_id"), str) else None,
                               token=tok if isinstance(tok, str) else None)
    if res.get("error") == "unknown_scene":
        return JSONResponse({"error": "unknown_scene", "message": "no such scene"}, status_code=404)
    return JSONResponse(res, status_code=200 if res["executed"] else 409)


def scene_confirmation(hub: Hub, r: Request, b: dict):
    return hub.scenes.action_for_scene(r.path_params["scene_id"])


# ------------------------------------------------------------------------------------------------ registry / health
def rooms(hub: Hub, r: Request, b: dict):
    return hub.registry.rooms()


def registry_version(hub: Hub, r: Request, b: dict):
    return {"version": hub.registry.version, "last_event_seq": hub.bus.last_seq}


def health(hub: Hub, r: Request, b: dict):
    nodes = hub.registry.list_nodes()
    return {"ok": True, "mqtt_connected": bool(hub.transport.connected), "registry_version": hub.registry.version,
            "nodes": len(nodes), "nodes_online": sum(1 for n in nodes if n["online"]),
            "pending_nodes": sum(1 for n in nodes if n["status"] == "pending"),
            "pending_acks": hub.bridge.pending_count(), "announce_rejected": hub.bridge.stats["announce_rejected"]}


async def events(request: Request):
    hub: Hub = request.app.state.hub
    types_q = request.query_params.get("types")
    types = frozenset(t for t in (types_q or "").split(",") if t) if types_q else DEFAULT_TYPES
    if not types <= ALL_TYPES:
        return _err("bad_request", f"types must be within {sorted(ALL_TYPES)}", 400)
    since = request.headers.get("last-event-id") or request.query_params.get("since")
    try:
        since_seq = int(since) if since is not None else None
    except ValueError:
        return _err("bad_request", "since must be an integer", 400)
    sub = hub.bus.subscribe(types, since_seq)

    async def gen():
        try:
            yield f"retry: 2000\nevent: hello\ndata: {json.dumps({'version': hub.registry.version, 'last_event_seq': hub.bus.last_seq})}\n\n"
            while True:
                ev = await asyncio.to_thread(sub.get, 5.0)
                if ev is None:
                    yield ": keepalive\n\n"
                    continue
                yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
        finally:
            sub.close()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------------------------------------ mock-only routes
def mock_mint(hub: Hub, r: Request, b: dict):
    """Mock mode only: stands in for the gateway's POST /confirmations (direct flow)."""
    gw = r.app.state.fake_gateway
    action = b.get("action")
    if not isinstance(action, dict) or not isinstance(b.get("user_id"), str):
        raise HubError("bad_request", "body must be {user_id, action}")
    return {"confirmation_token": gw.mint(b["user_id"], action_hash(action)), "action_hash": action_hash(action)}


# ------------------------------------------------------------------------------------------------ auth
class CallerAuth:
    """Optional per-caller shared-secret check. verifier_dir holds <service>.sha256 files (hex SHA-256 of the secret).
    gateway: everything except /mcp.  agent: /mcp, /events, /registry/version, /health only."""

    def __init__(self, verifier_dir: str) -> None:
        self.verifiers: dict[str, bytes] = {}
        for p in Path(verifier_dir).glob("*.sha256"):
            self.verifiers[p.stem] = bytes.fromhex(p.read_text().strip())

    def caller(self, header: str | None) -> str | None:
        if not header:
            return None
        digest = hashlib.sha256(header.encode()).digest()
        found = None
        for name, v in self.verifiers.items():
            if hmac.compare_digest(digest, v):
                found = name
        return found

    @staticmethod
    def allowed(caller: str, path: str) -> bool:
        if caller == "gateway":
            return not path.startswith("/mcp")
        if caller == "agent":
            return any(path == p or path.startswith(p + "/") for p in AGENT_ALLOWED)
        return False


class AsgiGate:
    """Outermost ASGI app: caller auth, then /mcp -> MCP session manager, everything else -> Starlette routes."""

    def __init__(self, routes_app: Starlette, mcp_handler, auth: CallerAuth | None) -> None:
        self.app, self.mcp, self.auth = routes_app, mcp_handler, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if scope["type"] == "http":
            if self.auth is not None and path != "/health":
                hdrs = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                caller = self.auth.caller(hdrs.get("x-service-secret"))
                if caller is None or not self.auth.allowed(caller, path):
                    return await _send_json(send, 401, {"error": "unauthorized", "message": "missing or unscoped X-Service-Secret"})
            if path == "/mcp" or path.startswith("/mcp/"):
                return await self.mcp(scope, receive, send)
        return await self.app(scope, receive, send)


async def _send_json(send, status: int, doc: dict) -> None:
    body = json.dumps(doc).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def create_app(hub: Hub, mock_gateway=None, allowed_hosts: list[str] | None = None):
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = allowed_hosts or ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    manager = StreamableHTTPSessionManager(
        app=build_mcp_server(hub.executor, hub.scenes), json_response=True, stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                    allowed_origins=[]))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    R = Route
    routes = [
        R("/health", handler(health), methods=["GET"]),
        R("/registry/version", handler(registry_version), methods=["GET"]),
        R("/events", events, methods=["GET"]),
        R("/rooms", handler(rooms), methods=["GET"]),
        R("/devices", handler(list_devices), methods=["GET"]),
        R("/devices/{endpoint_id}", handler(get_device), methods=["GET"]),
        R("/devices/{endpoint_id}/set", handler(set_device, "required"), methods=["POST"]),
        R("/devices/{endpoint_id}/action", handler(set_action, "required"), methods=["POST"]),
        R("/scenes", handler(list_scenes), methods=["GET"]),
        R("/scenes", handler(create_scene, "required"), methods=["POST"]),
        R("/scenes/{scene_id}", handler(get_scene), methods=["GET"]),
        R("/scenes/{scene_id}", handler(update_scene, "required"), methods=["PUT"]),
        R("/scenes/{scene_id}", handler(delete_scene), methods=["DELETE"]),
        R("/scenes/{scene_id}/run", handler(run_scene, "optional"), methods=["POST"]),
        R("/scenes/{scene_id}/confirmation", handler(scene_confirmation), methods=["GET"]),
        R("/admin/provision-sessions", handler(create_provision_session, "required"), methods=["POST"]),
        R("/admin/provision-sessions", handler(list_sessions), methods=["GET"]),
        R("/admin/nodes", handler(list_nodes), methods=["GET"]),
        R("/admin/nodes/{node_id}", handler(get_node), methods=["GET"]),
        R("/admin/nodes/{node_id}", handler(forget_node), methods=["DELETE"]),
        R("/admin/nodes/{node_id}/approve", handler(approve_node, "required"), methods=["POST"]),
        R("/admin/nodes/{node_id}/identify", handler(identify_node, "optional"), methods=["POST"]),
        R("/admin/nodes/{node_id}/unprovision", handler(unprovision_node), methods=["POST"]),
        R("/admin/endpoints/{endpoint_id}", handler(patch_endpoint, "required"), methods=["PATCH"]),
        R("/admin/endpoints/{endpoint_id}/rebind", handler(rebind_endpoint, "required"), methods=["POST"]),
        R("/admin/audit", handler(audit), methods=["GET"]),
        R("/admin/rejections", handler(rejections), methods=["GET"]),
    ]
    if mock_gateway is not None:
        routes.append(R("/mock/mint", handler(mock_mint, "required"), methods=["POST"]))
    inner = Starlette(routes=routes, lifespan=lifespan)
    inner.state.hub = hub
    inner.state.fake_gateway = mock_gateway
    auth = CallerAuth(hub.cfg.auth.verifier_dir) if hub.cfg.auth.verifier_dir else None
    if auth is None:
        log.warning("API caller auth is OFF (auth.verifier_dir not set): acceptable on loopback in dev only")
    return AsgiGate(inner, manager.handle_request, auth)
