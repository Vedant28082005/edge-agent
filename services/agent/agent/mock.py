"""--mock mode: a scripted LLM and a fake device-hub (MCP + /events) that speak the real contracts, served on 127.0.0.1
(ephemeral port) inside the agent process. No external network. Also used by the unit and end-to-end tests."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .llm import LlmResult
from .schema import validate_call
from .util import action_hash, est_tokens

AGENT_TYPES = {"registry_changed", "node_status", "state", "action_settled", "action_settle_timeout"}
AGENT_DEFAULT = AGENT_TYPES - {"state"}


# ======================================================================================================== local server
class LocalServer:
    """Runs an ASGI app on 127.0.0.1 with an ephemeral port in the current event loop."""

    def __init__(self, app, port: int = 0) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
                                                    timeout_graceful_shutdown=1, access_log=False))
        self._task: asyncio.Task | None = None
        self.port = 0

    async def start(self) -> str:
        self._task = asyncio.get_running_loop().create_task(self.server.serve())
        while not self.server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        self.server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except asyncio.TimeoutError:
                self.server.force_exit = True
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)


# ======================================================================================================== fake hub
NL = chr(10)


def _frame(e: dict) -> str:
    return f"id: {e['seq']}{NL}event: {e['type']}{NL}data: {json.dumps(e)}{NL}{NL}"


def _cap(cid, kind, typ, **kw):
    return {"id": cid, "kind": kind, "type": typ, **kw}


def demo_devices() -> list[dict]:
    return [
        {"endpoint_id": "ep_a1000001", "name": "Ceiling light", "room": "Bedroom", "type": "light", "tags": ["night"],
         "online": True, "risk": "normal", "capabilities": [_cap("power", "actuator", "bool"),
                                                           _cap("brightness", "actuator", "int", min=0, max=100)]},
        {"endpoint_id": "ep_a1000002", "name": "Fan", "room": "Bedroom", "type": "fan", "tags": [],
         "online": True, "risk": "normal", "capabilities": [_cap("power", "actuator", "bool"),
                                                           _cap("speed", "actuator", "int", min=1, max=5)]},
        {"endpoint_id": "ep_a1000003", "name": "Temp sensor", "room": "Bedroom", "type": "sensor", "tags": [],
         "online": True, "risk": "normal", "capabilities": [_cap("temperature", "sensor", "float", unit="C")]},
        {"endpoint_id": "ep_a1000004", "name": "Front door", "room": "Hall", "type": "lock", "tags": [],
         "online": True, "risk": "confirm", "capabilities": [_cap("lock", "actuator", "enum", values=["locked", "unlocked"])]},
        {"endpoint_id": "ep_a1000005", "name": "Blinds", "room": "Living room", "type": "cover", "tags": [],
         "online": True, "risk": "normal", "capabilities": [_cap("position", "actuator", "int", min=0, max=100)]},
        {"endpoint_id": "ep_a1000006", "name": "Lamp", "room": "Living room", "type": "light", "tags": [],
         "online": True, "risk": "normal", "capabilities": [_cap("power", "actuator", "bool")]},
    ]


class FakeHub:
    """Speaks POST /mcp (JSON-RPC: initialize, tools/list, tools/call with `_meta`) and GET /events (SSE, hello first,
    Last-Event-ID replay, 5 s keepalive, agent allowlist) with the agent secret. Records every call for assertions."""

    def __init__(self, secret: str, devices: list[dict] | None = None, settle_delay: float = 0.3) -> None:
        self.secret = secret
        self.devices = {d["endpoint_id"]: d for d in (devices or demo_devices())}
        self.state: dict[str, dict] = {ep: {} for ep in self.devices}
        self.version = 1
        self.settle_delay = settle_delay
        self.calls: list[dict] = []                 # {tool, args, meta}
        self.connections: list[dict] = []           # headers/query of each /events connection
        self.ring: list[dict] = []
        self.seq = 0
        self.tokens: dict[str, tuple[str, str]] = {}    # token -> (user_id, action_hash)
        self.settle_mode = "ok"                         # ok | timeout | silent
        self._subs: list[asyncio.Queue] = []
        self._tasks: set[asyncio.Task] = set()
        self.fail_next: list[str] = []                  # error codes returned by the next set_state calls
        self.send_registry_version = True               # False = a pre-1.4.0 hub (no registry_version / scenes in list_devices)
        self.send_scenes = True

    # -------------------------------------------------------------------------------------------- events
    def publish(self, etype: str, data: dict) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "type": etype, "ts": int(time.time()), "data": data}
        self.ring = (self.ring + [ev])[-1000:]
        for q in list(self._subs):
            q.put_nowait(ev)
        return ev

    def drop_streams(self) -> None:
        for q in list(self._subs):
            q.put_nowait(None)

    def registry_changed(self, reason: str = "patch", ids: list[str] | None = None) -> None:
        self.version += 1
        self.publish("registry_changed", {"version": self.version, "reason": reason, "endpoint_ids": ids or []})

    def rename(self, ep: str, name: str) -> None:
        self.devices[ep]["name"] = name
        self.registry_changed("patch", [ep])

    def remove(self, ep: str) -> None:
        self.devices.pop(ep)
        self.registry_changed("forget", [ep])

    # -------------------------------------------------------------------------------------------- tokens
    def mint(self, user_id: str, action: dict) -> str:
        t = secrets.token_urlsafe(32)
        self.tokens[t] = (user_id, action_hash(action))
        return t

    # -------------------------------------------------------------------------------------------- scenes
    SCENES = {"sc_9a8b7c6d": ("Good night", [("ep_a1000002", "power", False), ("ep_a1000004", "lock", "locked")])}

    def run_scene(self, scene: str, meta: dict) -> dict:
        sid = next((i for i, (n, _) in self.SCENES.items() if scene in (i, n)), None)
        if sid is None:
            return {"ok": False, "executed": False, "error": "unknown_scene"}
        name, acts = self.SCENES[sid]
        risky = sorted(({"endpoint_id": e, "capability": c, "value": v} for e, c, v in acts
                        if self.devices[e]["risk"] == "confirm"), key=lambda x: (x["endpoint_id"], x["capability"]))
        if risky:
            h = action_hash({"actions": risky, "scene_id": sid, "type": "run_scene"})
            tok = meta.get("confirmation_token")
            if not (tok in self.tokens and self.tokens[tok] == (meta.get("user_id"), h)):
                self.tokens.pop(tok, None)
                return {"ok": False, "executed": False, "status": "needs_confirmation", "scene_id": sid,
                        "needs_confirmation": risky, "action_hash": h}
            del self.tokens[tok]
        res = []
        for e, c, v in acts:
            self.state[e][c] = v
            res.append({"endpoint_id": e, "capability": c, "status": "done"})
        return {"ok": True, "executed": True, "scene": sid, "scene_name": name, "results": res}

    # -------------------------------------------------------------------------------------------- MCP
    def _auth(self, request: Request) -> bool:
        return request.headers.get("x-service-secret") == self.secret

    async def mcp(self, request: Request):
        if not self._auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        msg = await request.json()
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            res: Any = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-hub"}}
        elif method == "tools/list":
            res = {"tools": [{"name": n} for n in ("list_devices", "get_state", "set_state", "run_scene", "read_sensor")]}
        elif method == "tools/call":
            payload = self.tool(params.get("name"), params.get("arguments") or {}, params.get("_meta") or {})
            res = {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload, "isError": False}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": res})

    def tool(self, name: str, a: dict, meta: dict) -> dict:
        self.calls.append({"tool": name, "args": dict(a), "meta": dict(meta)})
        if name == "list_devices":
            devs = [d for d in self.devices.values() if (not a.get("room") or d["room"] == a["room"])]
            out: dict = {"devices": devs}
            if self.send_registry_version:                  # device-hub 1.4.0: registry_version + the read-only scenes listing
                out = {"registry_version": self.version, "devices": devs,
                       "scenes": [{"scene_id": i, "name": n} for i, (n, _) in self.SCENES.items()] if self.send_scenes else []}
            return out
        if name == "get_state":
            eps = [a["endpoint_id"]] if "endpoint_id" in a else [e for e, d in self.devices.items() if d["room"] == a.get("room")]
            if "endpoint_id" in a and a["endpoint_id"] not in self.devices:
                return {"ok": False, "error": "unknown_endpoint"}
            return {"states": [{"endpoint_id": e, "online": True, "values": self.state[e], "ts": int(time.time())} for e in eps]}
        if name == "read_sensor":
            d = self.devices.get(a["endpoint_id"])
            if not d:
                return {"ok": False, "error": "unknown_endpoint"}
            return {"endpoint_id": a["endpoint_id"], "capability": a["capability"], "value": 24.5, "unit": "C",
                    "ts": int(time.time()), "stale": False}
        if name == "run_scene":
            return self.run_scene(a["scene"], meta)
        if name != "set_state":
            return {"ok": False, "error": "unknown_tool"}
        if self.fail_next:
            return {"ok": False, "error": self.fail_next.pop(0)}
        d = self.devices.get(a["endpoint_id"])
        if not d:
            return {"ok": False, "error": "unknown_endpoint"}
        if d["risk"] == "confirm":
            tok = meta.get("confirmation_token")
            act = {"capability": a["capability"], "endpoint_id": a["endpoint_id"], "type": "set_state", "value": a["value"]}
            ok = tok in self.tokens and self.tokens[tok] == (meta.get("user_id"), action_hash(act))
            if tok in self.tokens:
                del self.tokens[tok]                       # single use (a mismatch burns it, as the contract says)
            if not ok:
                return {"ok": False, "error": "confirmation_required"}
        cid = meta.get("call_id")
        base = {"ok": True, "endpoint_id": a["endpoint_id"], "capability": a["capability"], "value": a["value"]}
        self.state[a["endpoint_id"]][a["capability"]] = a["value"] if a["capability"] != "position" else self.state[a["endpoint_id"]].get("position", 0)
        if a["capability"] == "position":
            t = asyncio.get_running_loop().create_task(self._settle(a, cid))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
            return {**base, "status": "moving", "verified": False}
        return {**base, "status": "done", "verified": True}

    async def _settle(self, a: dict, call_id: str | None) -> None:
        await asyncio.sleep(self.settle_delay)
        ep = a["endpoint_id"]
        if self.settle_mode == "silent":
            return
        extra = {"call_id": call_id} if call_id else {}
        if self.settle_mode == "timeout":
            self.publish("action_settle_timeout", {"endpoint_id": ep, "capability": "position", "value": a["value"],
                                                   "observed": 10, "settle_s": 30, **extra})
            return
        self.state[ep]["position"] = a["value"]
        self.publish("action_settled", {"endpoint_id": ep, "capability": "position", "value": a["value"],
                                        "observed": a["value"], "elapsed_ms": int(self.settle_delay * 1000), **extra})

    # -------------------------------------------------------------------------------------------- SSE
    async def events(self, request: Request):
        if not self._auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        types_q = request.query_params.get("types")
        types = set(types_q.split(",")) if types_q else set(AGENT_DEFAULT)
        if not types <= AGENT_TYPES:
            return JSONResponse({"error": "forbidden_event_type"}, status_code=403)
        last = request.headers.get("last-event-id")
        self.connections.append({"last_event_id": last, "types": sorted(types)})
        since = int(last) if last and last.isdigit() else None
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        sent = since if since is not None else self.seq
        replay = [e for e in self.ring if e["seq"] > sent and e["type"] in types]

        async def gen():
            nonlocal sent
            try:
                hello = json.dumps({"version": self.version, "last_event_seq": self.seq})
                yield f"retry: 200{NL}event: hello{NL}data: {hello}{NL}{NL}"
                for e in replay:
                    sent = e["seq"]
                    yield _frame(e)
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), 5.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive" + NL + NL
                        continue
                    if e is None:
                        return
                    if e["type"] in types and e["seq"] > sent:
                        sent = e["seq"]
                        yield _frame(e)
            finally:
                if q in self._subs:
                    self._subs.remove(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/mcp", self.mcp, methods=["POST"]), Route("/events", self.events)])


# ======================================================================================================== scripted LLM
_DEV = re.compile(r"^(ep_[0-9a-f]{8})\|([^|]*)\|([^|]*)\|([^|]*)\|(.*)$")
_STOP = {"the", "a", "an", "in", "of", "please", "to", "my"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP}


class ScriptedLLM:
    """Deterministic stand-in for llama-server. Reads the device list out of the prompt (the same lines the real model
    sees) and answers a few phrasings; `[hallucinate]` in the text makes it name a device that does not exist, so the
    constrained/unconstrained difference is testable. In constrained mode it simulates the grammar: a reply that does
    not satisfy the schema is replaced by an empty plan (a real grammar makes such a reply impossible)."""

    def __init__(self, constrained: bool = True) -> None:
        self.constrained = constrained
        self.mock = True
        self.requests: list[list[dict]] = []
        self._prev = ""
        self.delay = 0.0
        self.script: list[tuple[str, Any]] = []         # optional overrides: (regex on user text, reply dict|callable)

    async def complete(self, messages: list[dict], schema: dict | None) -> LlmResult:
        self.requests.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.respond(messages)
        if self.constrained and schema is not None:
            import jsonschema
            try:
                jsonschema.validate(reply, schema)
            except jsonschema.ValidationError:
                reply = {"calls": [], "say": "I can't do that."}
        cur = "".join(f"<{m['role']}>{m['content']}\n" for m in messages)
        common = os.path.commonprefix([self._prev, cur]) if self._prev else ""
        pn, cn = est_tokens(cur), est_tokens(common)
        self._prev = cur
        return LlmResult(json.dumps(reply), {"prompt_n": pn, "cache_n": cn, "prompt_ms": (pn - cn) * 10.0, "predicted_n": 30})

    # ------------------------------------------------------------------------------------------ the script
    def respond(self, messages: list[dict]) -> dict:
        last = messages[-1]["content"]
        text = next((ln[6:] for m in reversed(messages) for ln in reversed(m["content"].splitlines())
                     if m["role"] == "user" and ln.startswith("User: ")), "")
        for pat, rep in self.script:
            if re.search(pat, text, re.I):
                return rep(messages) if callable(rep) else rep
        if last.startswith("Tool results:"):
            m = re.search(r"temperature=([\d.]+)", last)
            if m:
                return {"calls": [], "say": f"It is {m.group(1)} degrees."}
            return {"calls": [], "say": "Here is what I found."}
        devs = self._devices(messages)
        low = text.lower()
        if "[hallucinate]" in low:
            return {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_dead0000", "capability": "power", "value": True}}],
                    "say": "Turning it on."}
        legend = next((ln for m in messages for ln in m["content"].splitlines() if ln.startswith("Capabilities by type:")), "")
        if re.search(r"good night|run .*scene", low):
            return {"calls": [{"tool": "run_scene", "args": {"scene": "Good night"}}], "say": "Running Good night."}
        m = re.search(r"\b(unlock|lock)\b", low)
        if m:
            d = self._find(devs, low, "lock")
            if d:
                val = "unlocked" if m.group(1) == "unlock" else "locked"
                return self._set(d, "lock", val, f"{'Unlocking' if val == 'unlocked' else 'Locking'} the {d[1].lower()}.")
        m = re.search(r"turn (on|off)|switch (on|off)", low)
        if m:
            on = "on" in (m.group(1) or m.group(2))
            d = self._find(devs, low)
            if d:
                return self._set(d, "power", on, f"Turning {'on' if on else 'off'} the {d[1].lower()}.")
        m = re.search(r"\b(open|close)\b", low)
        if m:
            d = self._find(devs, low, "cover")
            if d:
                return self._set(d, "position", 100 if m.group(1) == "open" else 0, f"{m.group(1).capitalize()}ing the {d[1].lower()}.")
        m = re.search(r"(?:set|dim)\b.*?(\d+)", low)
        if m:
            d = self._find(devs, low)
            if d:
                cap = "speed" if d[3] == "fan" else ("position" if d[3] == "cover" else "brightness")
                return self._set(d, cap, int(m.group(1)), f"Setting the {d[1].lower()}.")
        if re.search(r"temperature|how (hot|warm|cold)", low):
            d = self._find(devs, low, "sensor")
            if d and "temperature" in legend:
                return {"calls": [{"tool": "read_sensor", "args": {"endpoint_id": d[0], "capability": "temperature"}}],
                        "say": "Checking."}
        return {"calls": [], "say": "I'm not sure which device you mean."}

    @staticmethod
    def _set(d, cap, val, say) -> dict:
        return {"calls": [{"tool": "set_state", "args": {"endpoint_id": d[0], "capability": cap, "value": val}}], "say": say}

    @staticmethod
    def _devices(messages: list[dict]) -> list[tuple]:
        out = {}
        for m in messages:
            for ln in m["content"].splitlines():
                mm = _DEV.match(ln)
                if mm:
                    out[mm.group(1)] = mm.groups()
        return list(out.values())

    @staticmethod
    def _find(devs, low: str, typ: str | None = None):
        words = _words(low)
        best, best_s, tie = None, (0 if typ else 1), False
        for d in devs:
            if typ and d[3] != typ:
                continue
            s = len(_words(d[1]) & words) + (1 if _words(d[2]) <= words else 0) + (1 if d[3] in words else 0)
            if s > best_s:
                best, best_s, tie = d, s, False
            elif s == best_s and best is not None:
                tie = True
        return None if tie else best
