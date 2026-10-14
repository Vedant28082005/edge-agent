"""Hub access with the AGENT secret: an MCP client for POST /mcp (exactly the 5 runtime tools) and an SSE consumer for
GET /events (agent scope: server-side allowlist) with reconnect and Last-Event-ID replay.

The MCP client is a thin JSON-RPC-over-HTTP client (the hub serves stateless streamable HTTP with JSON responses) so
the executor has full control of `_meta` (confirmation_token, user_id, call_id), which the high-level SDK client does
not expose per call. The secret is sent only in the X-Service-Secret header and is never logged."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("agent.hub")

TOOLS = ("list_devices", "get_state", "set_state", "run_scene", "read_sensor")      # the only tools, ever
AGENT_EVENT_TYPES = ("registry_changed", "node_status", "action_settled", "action_settle_timeout")   # + state (opt-in)


class HubUnavailable(Exception):
    """Transport-level failure talking to the hub (connect/timeout/5xx/auth)."""


class McpClient:
    def __init__(self, base_url: str, secret: str, http: httpx.AsyncClient | None = None, timeout_s: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=3.0))
        self._own_http = http is None
        self._id = 0

    def _headers(self) -> dict[str, str]:
        return {"X-Service-Secret": self._secret, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        try:
            r = await self._http.post(self._base + "/mcp", headers=self._headers(), content=json.dumps(body))
        except httpx.HTTPError as e:
            raise HubUnavailable(f"hub request failed: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            raise HubUnavailable(f"hub refused the agent credentials (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise HubUnavailable(f"hub HTTP {r.status_code}")
        msg = _parse_rpc_response(r, self._id)
        if "error" in msg:
            raise HubUnavailable(f"hub rpc error {msg['error'].get('code')}")
        return msg.get("result") or {}

    async def list_tools(self) -> list[str]:
        res = await self._rpc("tools/list")
        return [t.get("name") for t in res.get("tools", []) if isinstance(t, dict)]

    async def verify_tools(self) -> None:
        """The hub must expose exactly the five tools. Anything else is logged; we never call a tool outside TOOLS."""
        names = await self.list_tools()
        if sorted(names) != sorted(TOOLS):
            log.error("hub exposes tools %s, expected exactly %s; only the expected ones will be used", names, list(TOOLS))

    async def call_tool(self, name: str, arguments: dict, meta: dict[str, str] | None = None) -> dict:
        if name not in TOOLS:
            raise ValueError(f"tool {name!r} is not one of the 5 runtime tools")
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if meta:
            params["_meta"] = meta
        res = await self._rpc("tools/call", params)
        return _tool_payload(res)


def _parse_rpc_response(r: httpx.Response, rpc_id: int) -> dict:
    ctype = r.headers.get("content-type", "")
    try:
        if ctype.startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    m = json.loads(line[5:].strip())
                    if isinstance(m, dict) and m.get("id") == rpc_id:
                        return m
            raise HubUnavailable("no rpc response in event stream")
        m = r.json()
    except (json.JSONDecodeError, ValueError):
        raise HubUnavailable("hub returned a non-JSON response") from None
    if not isinstance(m, dict):
        raise HubUnavailable("hub returned an unexpected response")
    return m


def _tool_payload(res: dict) -> dict:
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        out = sc.get("result") if set(sc) == {"result"} and isinstance(sc.get("result"), dict) else sc
        return out
    for block in res.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                v = json.loads(block.get("text", ""))
            except json.JSONDecodeError:
                break
            if isinstance(v, dict):
                return v
    return {"ok": False, "error": "tool_error"} if res.get("isError") else {"ok": False, "error": "bad_tool_result"}


# --------------------------------------------------------------------------------------------------- SSE consumer
class HubEventStream:
    """GET /events with the agent secret. Reconnects with jittered exponential backoff; sends Last-Event-ID on every
    reconnect. A `hello` whose last_event_seq is lower than our last id means the hub restarted (ids are monotonic per
    run), so the id is dropped and the caller is told to resync. `on_event(type, seq, data)` gets `data.data`."""

    def __init__(self, base_url: str, secret: str, on_event: Callable[[str, int, dict], Awaitable[None]],
                 on_hello: Callable[[int | None, bool], Awaitable[None]], http: httpx.AsyncClient | None = None,
                 include_state: bool = False, idle_timeout_s: float = 20.0, max_backoff_s: float = 15.0) -> None:
        self._base, self._secret = base_url.rstrip("/"), secret
        self._on_event, self._on_hello = on_event, on_hello
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(idle_timeout_s, connect=3.0))
        self._own_http = http is None
        self.types = list(AGENT_EVENT_TYPES) + (["state"] if include_state else [])
        self.last_id: int | None = None
        self.connected = False
        self.reconnects = 0
        self._max_backoff = max_backoff_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="hub-events")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=0.2)
        if self._own_http:
            await self._http.aclose()            # drops a connection the cancelled task could not close itself
        if self._task and not self._task.done():
            await asyncio.wait({self._task}, timeout=2.0)

    async def run(self) -> None:
        backoff = 0.25
        while not self._stop.is_set():
            ok_session = False
            try:
                ok_session = await self._session()
            except asyncio.CancelledError:
                raise
            except HubUnavailable as e:
                log.warning("event stream: %s", e)
            except httpx.HTTPError as e:
                log.warning("event stream dropped: %s", type(e).__name__)
            except Exception:
                log.exception("event stream error")
            self.connected = False
            self.reconnects += 1
            backoff = 0.25 if ok_session else min(self._max_backoff, backoff * 2)
            await asyncio.sleep(backoff * (0.7 + 0.6 * random.random()))

    async def _session(self) -> bool:
        headers = {"X-Service-Secret": self._secret, "Accept": "text/event-stream"}
        if self.last_id is not None:
            headers["Last-Event-ID"] = str(self.last_id)
        params = {"types": ",".join(self.types)}
        got_hello = False
        async with self._http.stream("GET", self._base + "/events", headers=headers, params=params) as r:
            if r.status_code in (401, 403):
                raise HubUnavailable(f"event stream refused (HTTP {r.status_code})")
            if r.status_code != 200:
                raise HubUnavailable(f"event stream HTTP {r.status_code}")
            ev_type, ev_id, data_lines = "message", None, []
            async for line in r.aiter_lines():
                if line == "":
                    if data_lines:
                        got_hello |= await self._frame(ev_type, ev_id, "\n".join(data_lines))
                    ev_type, ev_id, data_lines = "message", None, []
                elif line.startswith(":"):
                    continue                                    # keepalive comment
                elif line.startswith("event:"):
                    ev_type = line[6:].strip()
                elif line.startswith("id:"):
                    ev_id = line[3:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        return got_hello

    async def _frame(self, ev_type: str, ev_id: str | None, data: str) -> bool:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            log.warning("event stream: undecodable frame ignored")
            return False
        if not isinstance(obj, dict):
            return False
        if ev_type == "hello":
            resync = False
            seq = obj.get("last_event_seq")
            if isinstance(seq, int) and self.last_id is not None and seq < self.last_id:
                self.last_id, resync = None, True                # hub restarted: our id is from a previous run
            self.connected = True
            await self._on_hello(obj.get("version") if isinstance(obj.get("version"), int) else None, resync)
            return True
        seq = obj.get("seq") if isinstance(obj.get("seq"), int) else (int(ev_id) if ev_id and ev_id.isdigit() else None)
        if seq is not None:
            self.last_id = seq
        payload = obj.get("data")
        etype = obj.get("type") or ev_type
        if isinstance(etype, str) and isinstance(payload, dict):
            await self._on_event(etype, seq or 0, payload)
        return False
