"""--mock: fake hub, agent, vision-node and yolo-node living in-process behind httpx ASGI transports (no network).

The fake hub is deliberately thin but honours the same rules the real one does for the gateway: X-Service-Secret,
canonical action hashes, and consuming `risk: confirm` tokens through the gateway's REAL internal listener app."""
from __future__ import annotations

import hashlib
import itertools
import json
import secrets
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config
from .context import Ctx, build_ctx
from .internal import create_internal_app
from .security import SECRETS, hash_password, sha256_hex


def canon(obj) -> str:
    """JCS (RFC 8785) for the shapes used here; integral floats print as ints. The real hub's implementation is authoritative."""
    def norm(x):
        if isinstance(x, float) and x.is_integer():
            return int(x)
        if isinstance(x, dict):
            return {k: norm(v) for k, v in x.items()}
        return [norm(v) for v in x] if isinstance(x, list) else x
    return json.dumps(norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def action_hash(action: dict) -> str:
    return hashlib.sha256(canon(action).encode("utf-8")).hexdigest()


def sse(frames: list[tuple[str, dict]]) -> StreamingResponse:
    async def g():
        for ev, d in frames:
            fid = f"id: {d['seq']}\n" if "seq" in d else ""
            yield f"{fid}event: {ev}\ndata: {json.dumps(d)}\n\n".encode()
    return StreamingResponse(g(), media_type="text/event-stream")


def _ep(i, type_, name, room, caps, state, risk="normal", enabled=True, online=True) -> dict:
    return {"endpoint_id": f"ep_{i:08x}", "node_id": "esp32-aabbccddeeff", "local_id": f"l{i}", "name": name, "room": room,
            "aliases": [], "tags": [], "type": type_, "risk": risk, "online": online, "enabled": enabled,
            "capabilities": caps, "state": state}


POWER = {"id": "power", "kind": "actuator", "type": "bool"}
LOCK = {"id": "lock", "kind": "actuator", "type": "enum", "values": ["locked", "unlocked"]}


class FakeHub:
    """Fake device-hub (device-hub-internal.yaml subset used by the gateway)."""

    def __init__(self, secret: str = "mock-gateway-secret-0000") -> None:
        self.secret = secret
        self.consume_client: httpx.AsyncClient | None = None
        self.devices = {e["endpoint_id"]: e for e in [
            _ep(1, "light", "Ceiling light", "Bedroom", [POWER], {"power": False}),
            _ep(2, "fan", "Fan", "Bedroom", [POWER], {"power": False}),
            _ep(3, "lock", "Front door", "Hall", [LOCK], {"lock": "locked"}, risk="confirm"),
            _ep(4, "light", "Kitchen light", "Kitchen", [POWER], {"power": False}),
            _ep(5, "light", "Old lamp", "Bedroom", [POWER], {"power": False}, enabled=False),
        ]}
        self.scenes: dict[str, dict] = {}
        self.add_scene("sc_00000001", "Good night", "u_seed", [
            {"target": {"endpoint_id": "ep_00000001"}, "capability": "power", "value": False}])
        self.add_scene("sc_00000002", "Open up", "u_seed", [
            {"target": {"endpoint_id": "ep_00000003"}, "capability": "lock", "value": "unlocked"},
            {"target": {"endpoint_id": "ep_00000001"}, "capability": "power", "value": True}])
        self.add_scene("sc_00000003", "Kitchen on", "u_seed", [
            {"target": {"endpoint_id": "ep_00000004"}, "capability": "power", "value": True}])
        self.add_scene("sc_00000004", "Everything off", "u_seed", [{"target": {"type": "light"}, "capability": "power", "value": False}])
        self.events: list[tuple[str, dict]] = []
        self.calls: list[dict] = []
        self._ids = itertools.count(5)
        self.app = self._build()

    # ---- helpers
    def add_scene(self, sid, name, owner, actions) -> dict:
        s = {"scene_id": sid, "name": name, "owner": owner, "actions": actions, "broken": False}
        s["rooms"] = self.rooms_of(actions)
        self.scenes[sid] = s
        return s

    def rooms_of(self, actions) -> list[str]:
        out = set()
        for a in actions:
            t = a["target"]
            out.add(self.devices[t["endpoint_id"]]["room"] if "endpoint_id" in t and t["endpoint_id"] in self.devices
                    else t.get("room", "*"))
        return sorted(out)

    def resolve(self, scene) -> list[tuple[dict, dict, str, object]]:
        out = []
        for a in scene["actions"]:
            t = a["target"]
            for ep in self.devices.values():
                if not ep["enabled"]:
                    continue
                if "endpoint_id" in t:
                    if ep["endpoint_id"] != t["endpoint_id"]:
                        continue
                elif any(k in t and (ep.get(k) != t[k] if k != "tag" else t[k] not in ep["tags"]) for k in ("room", "type", "tag")):
                    continue
                out.append((ep, a, a["capability"], a["value"]))
        return out

    def risky(self, scene) -> list[dict]:
        r = sorted({(ep["endpoint_id"], cap): val for ep, _a, cap, val in self.resolve(scene) if ep["risk"] == "confirm"}.items())
        return [{"endpoint_id": e, "capability": c, "value": v} for (e, c), v in r]

    def scene_action(self, scene) -> dict:
        return {"type": "run_scene", "scene_id": scene["scene_id"], "actions": self.risky(scene)}

    async def consume(self, token, user_id, ahash) -> bool:
        if not token or self.consume_client is None:
            return False
        r = await self.consume_client.post("/internal/confirmations/consume", json={"token": token, "user_id": user_id, "action_hash": ahash})
        return r.status_code == 200 and r.json().get("consumed") is True

    def push(self, typ: str, data: dict) -> None:
        self.events.append((typ, {"seq": len(self.events) + 1, "type": typ, "ts": 0, "data": data}))

    # ---- app
    def _build(self) -> FastAPI:
        app = FastAPI(docs_url=None, openapi_url=None)

        @app.middleware("http")
        async def auth(request: Request, call_next):
            self.calls.append({"method": request.method, "path": request.url.path, "actor": request.headers.get("x-actor")})
            if request.url.path != "/health" and request.headers.get("x-service-secret") != self.secret:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        @app.get("/health")
        async def health():
            return {"ok": True, "mqtt_connected": True, "registry_version": 1, "nodes": 1, "nodes_online": 1, "pending_acks": 0}

        @app.get("/events")
        async def events(request: Request):
            last = int(request.headers.get("last-event-id") or request.query_params.get("since") or 0)
            return sse([("hello", {"version": 1, "last_event_seq": 0})] + [(t, e) for t, e in self.events if e["seq"] > last])

        @app.get("/devices")
        async def devices(room: str | None = None, type: str | None = None):
            return [e for e in self.devices.values() if (not room or e["room"] == room) and (not type or e["type"] == type)]

        @app.get("/devices/{eid}")
        async def device(eid: str):
            return self.devices.get(eid) or JSONResponse({"error": "unknown_endpoint"}, status_code=404)

        @app.post("/devices/{eid}/action")
        async def dev_action(eid: str, request: Request):
            ep, b = self.devices.get(eid), await request.json()
            if ep is None:
                return JSONResponse({"error": "unknown_endpoint"}, status_code=404)
            a = {"type": "set_state", "endpoint_id": eid, "capability": b["capability"], "value": b["value"]}
            return {"action": a, "action_hash": action_hash(a), "risk": ep["risk"]}

        @app.post("/devices/{eid}/set")
        async def dev_set(eid: str, request: Request):
            ep, b = self.devices.get(eid), await request.json()
            if ep is None:
                return JSONResponse({"error": "unknown_endpoint"}, status_code=404)
            caps = {c["id"] for c in ep["capabilities"]}
            if b["capability"] not in caps:
                return {"ok": False, "error": "unknown_capability"}
            if not ep["online"]:
                return {"ok": False, "error": "offline"}
            if ep["risk"] == "confirm":
                a = {"type": "set_state", "endpoint_id": eid, "capability": b["capability"], "value": b["value"]}
                if not await self.consume(b.get("confirmation_token"), b.get("user_id", ""), action_hash(a)):
                    return {"ok": False, "error": "confirmation_required", "endpoint_id": eid}
            ep["state"][b["capability"]] = b["value"]
            return {"ok": True, "status": "done", "verified": True, "endpoint_id": eid, "capability": b["capability"], "value": b["value"]}

        @app.get("/scenes")
        async def scenes():
            return list(self.scenes.values())

        @app.post("/scenes", status_code=201)
        async def create_scene(request: Request):
            b = await request.json()
            sid = f"sc_{next(self._ids):08x}"
            return JSONResponse(self.add_scene(sid, b["name"], b["owner"], b["actions"]), status_code=201)

        @app.get("/scenes/{sid}")
        async def get_scene(sid: str):
            return self.scenes.get(sid) or JSONResponse({"error": "unknown_scene"}, status_code=404)

        @app.put("/scenes/{sid}")
        async def put_scene(sid: str, request: Request):
            b, s = await request.json(), self.scenes.get(sid)
            if s is None:
                return JSONResponse({"error": "unknown_scene"}, status_code=404)
            s.update({k: v for k, v in b.items() if k in ("name", "actions")})
            s["rooms"] = self.rooms_of(s["actions"])
            return s

        @app.delete("/scenes/{sid}")
        async def del_scene(sid: str):
            return Response(status_code=204) if self.scenes.pop(sid, None) else JSONResponse({"error": "unknown_scene"}, status_code=404)

        @app.get("/scenes/{sid}/confirmation")
        async def scene_conf(sid: str):
            s = self.scenes.get(sid)
            if s is None:
                return JSONResponse({"error": "unknown_scene"}, status_code=404)
            a = self.scene_action(s)
            return {"action": a, "action_hash": action_hash(a), "needs_confirmation": a["actions"]}

        @app.post("/scenes/{sid}/run")
        async def run(sid: str, request: Request):
            s, b = self.scenes.get(sid), await request.json()
            if s is None:
                return JSONResponse({"ok": False, "executed": False, "error": "unknown_scene"}, status_code=404)
            risky = self.risky(s)
            if risky:
                if not await self.consume(b.get("confirmation_token"), b.get("user_id", ""), action_hash(self.scene_action(s))):
                    return JSONResponse({"ok": False, "executed": False, "status": "needs_confirmation", "scene_id": sid,
                                         "needs_confirmation": risky, "action_hash": action_hash(self.scene_action(s))}, status_code=409)
            results = []
            for ep, _a, cap, val in self.resolve(s):
                ep["state"][cap] = val
                results.append({"endpoint_id": ep["endpoint_id"], "capability": cap, "status": "done"})
            return {"ok": True, "executed": True, "scene": sid, "scene_name": s["name"], "results": results}

        # ---- admin
        @app.post("/admin/provision-sessions", status_code=201)
        async def prov(request: Request):
            b = await request.json()
            return JSONResponse({"session_id": "ps_" + secrets.token_hex(4), "expires_at": 4102444800, "bundle": {
                "v": 1, "wifi": b["wifi"], "mqtt": {"host": "192.0.2.10", "port": 8883, "username": b["node_id"],
                                                    "password": "mqtt-" + secrets.token_hex(8)},
                "ca_pem": "-----BEGIN CERTIFICATE-----\nMOCK\n-----END CERTIFICATE-----\n"}}, status_code=201)

        @app.get("/admin/nodes")
        async def nodes():
            return [{"node_id": "esp32-aabbccddeeff", "status": "online", "hw": "esp32", "fw": "0.1.0"}]

        @app.post("/admin/nodes/{nid}/approve")
        async def approve(nid: str, request: Request):
            b = await request.json()
            if nid != "esp32-aabbccddeeff":
                return JSONResponse({"error": "unknown_node"}, status_code=404)
            return [{"endpoint_id": "ep_0000000a", "name": e["name"], "room": e["room"], "type": "light", "online": True,
                     "enabled": True, "capabilities": [POWER]} for e in b["endpoints"]]

        @app.post("/admin/nodes/{nid}/identify", status_code=202)
        async def identify(nid: str):
            return {"sent": True, "acked": True}

        @app.post("/admin/nodes/{nid}/unprovision", status_code=202)
        async def unprov(nid: str):
            return {"sent": True, "acked": True}

        @app.delete("/admin/nodes/{nid}")
        async def forget(nid: str, confirm: bool = False):
            if not confirm:
                return JSONResponse({"error": "confirmation_required", "scenes": 2, "automations": 0, "message": "2 scenes"}, status_code=409)
            return Response(status_code=204)

        @app.patch("/admin/endpoints/{eid}")
        async def patch_ep(eid: str, request: Request):
            ep = self.devices.get(eid)
            if ep is None:
                return JSONResponse({"error": "unknown_endpoint"}, status_code=404)
            ep.update(await request.json())
            return ep

        @app.post("/admin/endpoints/{eid}/rebind")
        async def rebind(eid: str, request: Request):
            ep = self.devices.get(eid)
            if ep is None:
                return JSONResponse({"error": "unknown_endpoint"}, status_code=404)
            ep.update(await request.json())
            return ep

        return app


CHAT_KEYS = {"user_id", "role", "session_id", "text", "images"}
DECISION_KEYS = {"decision", "user_id", "role", "action", "action_hash", "confirmation_token"}


class FakeAgent:
    """Fake agent (agent-internal.yaml): X-Service-Secret on every route, strict ChatRequest/ConfirmationDecision bodies."""

    def __init__(self, secret: str = "mock-agent-api-secret-0000") -> None:
        self.secret = secret
        self.requests: list[dict] = []
        self.deliveries: list[dict] = []
        self.push: list[tuple[str, dict]] = []          # (event name, data incl. user_id); ids are 1..n
        self.memories: dict[str, dict[str, str]] = {}
        self.fail_delivery = False
        self.delivery_status = 202
        self.calls: list[dict] = []
        self._n = itertools.count(1)
        self.app = self._build()

    def emit(self, name: str, data: dict) -> None:
        self.push.append((name, data))

    def _build(self) -> FastAPI:
        app = FastAPI(docs_url=None, openapi_url=None)

        @app.middleware("http")
        async def auth(request: Request, call_next):
            self.calls.append({"method": request.method, "path": request.url.path, "secret_ok": request.headers.get("x-service-secret") == self.secret})
            if request.headers.get("x-service-secret") != self.secret:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        @app.get("/v1/health")
        async def health():
            return {"ok": True, "llm": "mock", "hub": "mock"}

        @app.post("/v1/chat")
        async def chat(request: Request):
            b = await request.json()
            if not isinstance(b, dict) or set(b) - CHAT_KEYS or not {"user_id", "role", "session_id"} <= set(b) \
                    or len(b["session_id"]) > 64 or b["role"] not in ("admin", "member", "guest"):
                return JSONResponse({"error": "bad_request", "message": "invalid ChatRequest"}, status_code=400)
            self.requests.append(b)
            text, rid, n = b.get("text") or "", f"req-{next(self._n)}", 0

            def f(ev, d):
                nonlocal n
                n += 1
                return f"event: {ev}\ndata: {json.dumps({**d, 'seq': n, 'req_id': 'r1'})}\n\n"
            frames = [f("status", {"text": "thinking"})]
            if "unlock" in text:
                a = {"type": "set_state", "endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}
                frames.append(f("confirmation_request", {"request_id": rid, "summary": "Unlock front door?", "action": a,
                                                         "action_hash": action_hash(a), "expires_in_s": 60}))
            elif "open up" in text:
                a = {"type": "run_scene", "scene_id": "sc_00000002", "actions": [
                    {"endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}]}
                frames.append(f("confirmation_request", {"request_id": rid, "summary": "Open up?", "action": a,
                                                         "action_hash": action_hash(a), "expires_in_s": 60}))
            else:
                frames.append(f("token", {"text": "ok"}))
            frames.append(f("final", {"text": "done", "route": "fast", "latency_ms": 5}))

            async def g():
                for x in frames:
                    yield x.encode()
            return StreamingResponse(g(), media_type="text/event-stream")

        @app.post("/v1/confirmations/{rid}")
        async def deliver(rid: str, request: Request):
            b = await request.json()
            if not isinstance(b, dict) or set(b) - DECISION_KEYS or not {"decision", "user_id", "role", "action", "action_hash"} <= set(b) \
                    or b["decision"] not in ("approved", "declined", "expired") \
                    or (b["decision"] == "approved") != ("confirmation_token" in b):
                return JSONResponse({"error": "bad_request"}, status_code=400)
            if b["action_hash"] != action_hash(b["action"]):
                return JSONResponse({"error": "action_hash_mismatch"}, status_code=409)
            if self.fail_delivery:
                return JSONResponse({"error": "unknown_request"}, status_code=404)
            self.deliveries.append({"request_id": rid, **b})
            return JSONResponse({}, status_code=self.delivery_status)

        @app.get("/v1/events")
        async def events(request: Request):
            last = int(request.headers.get("last-event-id") or 0)
            frames = [(n, name, d) for n, (name, d) in enumerate(self.push, 1) if n > last]

            async def g():
                for n, name, d in frames:
                    yield f"id: {n}\nevent: {name}\ndata: {json.dumps(d)}\n\n".encode()
            return StreamingResponse(g(), media_type="text/event-stream")

        @app.get("/v1/memory")
        async def mem(user_id: str):
            return [{"memory_id": k, "text": v, "updated_at": 1} for k, v in self.memories.get(user_id, {}).items()]

        @app.put("/v1/memory/{mid}")
        async def mem_put(mid: str, request: Request):
            b = await request.json()
            if not isinstance(b, dict) or set(b) != {"user_id", "text"} or not isinstance(b["text"], str) \
                    or not 1 <= len(b["text"]) <= 500 or not isinstance(b["user_id"], str) or not 1 <= len(b["user_id"]) <= 64 \
                    or len(mid) > 64:
                return JSONResponse({"error": "bad_request"}, status_code=400)
            if mid not in self.memories.get(b["user_id"], {}):
                return JSONResponse({"error": "not_found"}, status_code=404)
            self.memories[b["user_id"]][mid] = b["text"]
            return {"memory_id": mid, "text": b["text"], "updated_at": 2}

        @app.delete("/v1/memory/{mid}")
        async def mem_del(mid: str, user_id: str):
            if mid not in self.memories.get(user_id, {}):
                return JSONResponse({"error": "not_found"}, status_code=404)
            del self.memories[user_id][mid]
            return Response(status_code=204)

        return app


class FakeVision:
    def __init__(self) -> None:
        self.received: list[int] = []
        self.fail = False
        app = FastAPI(docs_url=None, openapi_url=None)

        @app.get("/health")
        async def health():
            return {"mem_available_mb": 1500, "queue_depth": 0, "busy": False, "temp_c": 51.5, "models": ["whisper"]}

        @app.post("/transcribe")
        async def transcribe(request: Request):
            body = await request.body()
            self.received.append(len(body))
            if self.fail:
                return JSONResponse({"error": "vision_unavailable"}, status_code=503)
            return {"text": "turn on the bedroom light", "duration_ms": 900, "filtered": False}
        self.app = app


class FakeYolo:
    def __init__(self) -> None:
        app = FastAPI(docs_url=None, openapi_url=None)

        @app.get("/health")
        async def health():
            return {"mem_available_mb": 900, "queue_depth": 1, "temp_c": 60.0}
        self.app = app


def asgi(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


class Mock:
    def __init__(self, ctx: Ctx, hub: FakeHub, agent: FakeAgent, vision: FakeVision, yolo: FakeYolo, internal_app, hub_secret: str,
                 device_hub_secret: str) -> None:
        self.ctx, self.hub, self.agent, self.vision, self.yolo = ctx, hub, agent, vision, yolo
        self.internal_app, self.hub_secret, self.device_hub_secret = internal_app, hub_secret, device_hub_secret
        self.passwords: dict[str, str] = {}


def build_mock(cfg: Config | None = None, *, data_dir: str | None = None, clock=None, seed_users: bool = True) -> Mock:
    import time
    cfg = cfg or Config()
    cfg.mock = True
    cfg.data_dir = data_dir or tempfile.mkdtemp(prefix="gw-mock-")
    ver = Path(cfg.data_dir) / "verifiers"
    ver.mkdir(parents=True, exist_ok=True)
    dh_secret = secrets.token_urlsafe(32)
    (ver / "device-hub.sha256").write_text(sha256_hex(dh_secret), encoding="utf-8")
    cfg.verifier_dir = str(ver)
    SECRETS.add(dh_secret)
    hub, agent, vision, yolo = FakeHub(secrets.token_urlsafe(24)), FakeAgent(secrets.token_urlsafe(24)), FakeVision(), FakeYolo()
    SECRETS.add(agent.secret)
    ctx = build_ctx(cfg, transports={"hub": asgi(hub.app), "agent": asgi(agent.app), "vision": asgi(vision.app), "yolo": asgi(yolo.app)},
                    hub_secret=hub.secret, agent_secret=agent.secret, clock=clock or time.time, db_path=":memory:", agent_push=False)
    internal = create_internal_app(ctx)
    hub.consume_client = httpx.AsyncClient(base_url="http://gateway-internal", transport=asgi(internal),
                                           headers={"X-Service-Secret": dh_secret})
    m = Mock(ctx, hub, agent, vision, yolo, internal, hub.secret, dh_secret)
    if seed_users:
        for name, role, rooms in (("admin", "admin", []), ("member", "member", ["Bedroom", "Kitchen"]), ("guest", "guest", ["Bedroom"])):
            pw = secrets.token_urlsafe(12)
            SECRETS.add(pw)
            ctx.auth.create(name, hash_password(pw), role, rooms)
            m.passwords[name] = pw
    return m
