"""The consume client against a mock gateway built from contracts/samples/confirmation/consume-scenarios.json."""
from __future__ import annotations

import httpx
import pytest

from conftest import CONTRACTS, load_json
from device_hub.gateway_client import GatewayClient
from device_hub.mock import FakeGateway
from device_hub.util import ManualClock

SCENARIOS = load_json(CONTRACTS / "samples" / "confirmation" / "consume-scenarios.json")["scenarios"]


def run_scenario(sc, make_client):
    clock = ManualClock(1000.0)
    gw = FakeGateway(clock=clock)
    client = make_client(gw)
    token = gw.mint(sc["token"]["user"], sc["token"]["hash"], sc["token"]["ttl"]) if sc["token"] else "A" * 43
    for call in sc["calls"]:
        clock.t = 1000.0 + call["at"]
        res = client.consume(token, call["user"], call["hash"])
        if call["expect"] == "granted":
            assert res.granted, (sc["name"], call)
        else:
            assert not res.granted and res.reason == call["expect"], (sc["name"], call, res)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s["name"])
def test_scenarios_in_process(sc):
    run_scenario(sc, lambda gw: gw.client())


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s["name"])
def test_scenarios_over_real_http(sc):
    servers = []

    def make(gw):
        srv, url = gw.serve_http()
        servers.append(srv)
        return GatewayClient(url, gw.secret, 2.0)

    try:
        run_scenario(sc, make)
    finally:
        for s in servers:
            s.shutdown()


def test_wrong_service_secret_is_denied():
    gw = FakeGateway()
    client = GatewayClient("http://x", "not-the-secret-123", 1.0, transport=httpx.MockTransport(gw.handler))
    token = gw.mint("user:v", "a" * 64)
    res = client.consume(token, "user:v", "a" * 64)
    assert not res.granted and res.reason == "gateway_error"


def test_malformed_inputs_never_reach_the_gateway():
    gw = FakeGateway()
    c = gw.client()
    assert c.consume("short", "user:v", "a" * 64).reason == "invalid"
    assert c.consume("A" * 43, "", "a" * 64).reason == "invalid"
    assert c.consume("A" * 43, "x" * 65, "a" * 64).reason == "invalid"
    assert c.consume("A" * 43, "user:v", "A" * 64).reason == "invalid"      # uppercase hash
    assert gw.calls == []


def test_fails_closed_on_garbage_and_server_errors():
    def mk(handler):
        return GatewayClient("http://x", "s" * 40, 1.0, transport=httpx.MockTransport(handler))

    tok = "A" * 43
    h = "a" * 64
    assert not mk(lambda r: httpx.Response(200, json={})).consume(tok, "u", h).granted
    assert not mk(lambda r: httpx.Response(200, json={"consumed": True, "action_hash": "b" * 64, "user_id": "u"})).consume(tok, "u", h).granted
    assert not mk(lambda r: httpx.Response(200, json={"consumed": True, "action_hash": h, "user_id": "someone-else"})).consume(tok, "u", h).granted
    assert not mk(lambda r: httpx.Response(200, text="<html>")).consume(tok, "u", h).granted
    assert not mk(lambda r: httpx.Response(500)).consume(tok, "u", h).granted
    assert not mk(lambda r: httpx.Response(401, json={"error": "x"})).consume(tok, "u", h).granted
    assert mk(lambda r: httpx.Response(403, json={"consumed": False, "reason": "banana"})).consume(tok, "u", h).reason == "gateway_error"
    assert mk(lambda r: httpx.Response(200, json={"consumed": True, "action_hash": h, "user_id": "u"})).consume(tok, "u", h).granted


@pytest.mark.skipif(not hasattr(__import__("socket"), "AF_UNIX"), reason="no AF_UNIX on this platform")
def test_unix_socket_url(tmp_path):
    """gateway.url = unix:///path (the preferred internal listener in gateway.yaml)."""
    import socketserver
    import threading
    from http.server import BaseHTTPRequestHandler

    gw = FakeGateway()
    path = str(tmp_path / "gw.sock")

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            r = gw.handler(httpx.Request("POST", f"http://x{self.path}", headers=dict(self.headers), content=self.rfile.read(n)))
            self.send_response(r.status_code)
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)

        def address_string(self):
            return "unix"

        def log_message(self, *a):
            pass

    class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    try:
        srv = S(path, H)
    except OSError as e:                       # e.g. Windows without AF_UNIX server support
        pytest.skip(f"cannot create a unix socket server here: {e}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = GatewayClient("unix://" + path, gw.secret, 2.0)
        token = gw.mint("user:v", "a" * 64)
        assert client.consume(token, "user:v", "a" * 64).granted
        assert client.consume(token, "user:v", "a" * 64).reason == "used"
    finally:
        srv.shutdown()
