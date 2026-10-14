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
        m.ctx.agent = m.ctx.confirm.agent = AgentClient(agent.url)
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
