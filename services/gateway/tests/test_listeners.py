"""Real sockets: listener separation (/internal/* only on the internal one) and incremental SSE passthrough."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from gateway.app import create_public_app
from gateway.mock import action_hash, build_mock
from gateway.upstream import AgentClient


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Srv:
    def __init__(self, app, port):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        self.t = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{port}"

    def __enter__(self):
        self.t.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.t.join(5)


def test_internal_paths_exist_only_on_the_internal_listener():
    m = build_mock()
    pub_app, int_app = create_public_app(m.ctx), m.internal_app
    tok = m.ctx.confirm._mint("alice", action_hash({"a": 1}), None)[0]
    body = {"token": tok, "user_id": "alice", "action_hash": action_hash({"a": 1})}
    with Srv(pub_app, free_port()) as pub, Srv(int_app, free_port()) as internal:
        hdr = {"X-Service-Secret": m.device_hub_secret}
        for method in ("POST", "GET", "PUT", "DELETE"):
            for path in ("/internal/confirmations/consume", "/internal/", "/internal", "/internal/anything"):
                r = httpx.request(method, pub.url + path, json=body, headers=hdr)
                assert r.status_code == 404, (method, path, r.status_code)
        # a valid secret is useless on the public port: the token is still unspent
        assert m.ctx.db.one("SELECT used FROM confirm_tokens")["used"] == 0
        r = httpx.post(internal.url + "/internal/confirmations/consume", json=body, headers=hdr)
        assert r.status_code == 200 and r.json()["consumed"] is True
        assert httpx.post(internal.url + "/internal/confirmations/consume", json=body, headers=hdr).json()["reason"] == "used"
        # and the internal port serves nothing else
        assert httpx.get(internal.url + "/devices").status_code == 404
        assert httpx.post(internal.url + "/auth/login", json={"username": "admin", "password": "x"}).status_code == 404
        assert httpx.get(pub.url + "/").status_code == 200                          # placeholder web app
        assert "Edge Agent" in httpx.get(pub.url + "/").text


def test_chat_sse_is_streamed_not_buffered():
    slow = FastAPI()

    @slow.post("/v1/chat")
    async def chat():
        async def g():
            yield b"event: status\ndata: {\"text\": \"first\", \"seq\": 1}\n\n"
            await asyncio.sleep(1.5)
            yield b"event: final\ndata: {\"text\": \"done\", \"seq\": 2}\n\n"
        return StreamingResponse(g(), media_type="text/event-stream")

    m = build_mock()
    with Srv(slow, free_port()) as agent:
        m.ctx.agent = m.ctx.confirm.agent = AgentClient(agent.url, None)
        app = create_public_app(m.ctx)
        tok = httpx.Client()
        with Srv(app, free_port()) as gw:
            access = httpx.post(gw.url + "/auth/login", json={"username": "member", "password": m.passwords["member"]}).json()["access_token"]
            t0 = time.time()
            with httpx.stream("POST", gw.url + "/chat", json={"text": "hi"}, headers={"Authorization": "Bearer " + access}, timeout=10) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                lines = r.iter_lines()
                first = next(l for l in lines if l.startswith("data:"))
                t_first = time.time() - t0
                rest = [l for l in lines if l.startswith("data:")]
            assert json.loads(first[5:])["text"] == "first" and t_first < 1.0, f"first event only after {t_first:.2f}s (buffered?)"
            assert json.loads(rest[0][5:])["text"] == "done" and time.time() - t0 >= 1.4
        tok.close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets need Linux/macOS")
def test_consume_over_a_unix_socket(tmp_path):
    """The preferred internal transport: gateway-internal.sock (cfg.internal.uds), mode-restricted, no TCP port at all."""
    import os
    m = build_mock()
    path = str(tmp_path / "gateway-internal.sock")
    h = action_hash({"a": 1})
    tok = m.ctx.confirm._mint("alice", h, None)[0]
    srv = uvicorn.Server(uvicorn.Config(m.internal_app, uds=path, log_level="warning", access_log=False))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        assert srv.started and os.path.exists(path)
        with httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url="http://gateway-internal") as c:
            body = {"token": tok, "user_id": "alice", "action_hash": h}
            assert c.post("/internal/confirmations/consume", json=body).status_code == 401
            r = c.post("/internal/confirmations/consume", json=body, headers={"X-Service-Secret": m.device_hub_secret})
            assert r.status_code == 200 and r.json() == {"consumed": True, "action_hash": h, "user_id": "alice"}
            assert c.post("/internal/confirmations/consume", json=body,
                          headers={"X-Service-Secret": m.device_hub_secret}).json()["reason"] == "used"
            assert c.get("/devices").status_code == 404
    finally:
        srv.should_exit = True
        t.join(5)


def test_cli_serve_binds_the_internal_listener_to_a_unix_socket_when_configured(tmp_path):
    from gateway.config import load_config
    p = tmp_path / "c.json"
    p.write_text('{"internal": {"uds": "/run/edge-agent/gateway-internal.sock"}}', encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.internal.uds == "/run/edge-agent/gateway-internal.sock" and cfg.public.host == "127.0.0.1" and cfg.public.port == 8000


def test_chat_abort_closes_the_upstream_agent_request():
    """(S8o) The browser drops the SSE stream: the gateway must close its request to the agent, so the agent sees the
    disconnect and cancels the turn."""
    state = {"started": False, "cancelled": False}
    slow = FastAPI()

    @slow.post("/v1/chat")
    async def chat():
        async def g():
            state["started"] = True
            try:
                yield b"event: status\ndata: {\"text\": \"first\", \"seq\": 1}\n\n"
                await asyncio.sleep(30)
                yield b"event: final\ndata: {\"text\": \"done\", \"seq\": 2}\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                state["cancelled"] = True
                raise
        return StreamingResponse(g(), media_type="text/event-stream")

    m = build_mock()
    with Srv(slow, free_port()) as agent:
        m.ctx.agent = m.ctx.confirm.agent = AgentClient(agent.url, None)
        app = create_public_app(m.ctx)
        with Srv(app, free_port()) as gw:
            access = httpx.post(gw.url + "/auth/login", json={"username": "member", "password": m.passwords["member"]}).json()["access_token"]
            with httpx.stream("POST", gw.url + "/chat", json={"text": "hi"}, headers={"Authorization": "Bearer " + access}, timeout=10) as r:
                next(l for l in r.iter_lines() if l.startswith("data:"))
            for _ in range(100):                             # the browser is gone now
                if state["cancelled"]:
                    break
                time.sleep(0.05)
            assert state["started"] and state["cancelled"], "the agent request was not closed"
