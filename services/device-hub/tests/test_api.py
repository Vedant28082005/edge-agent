"""Internal HTTP API (served by a real uvicorn instance on loopback), SSE event stream, MCP over streamable HTTP,
caller auth."""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import WIFI, light_ep, make_announce
from device_hub.api import create_app
from device_hub.util import set_state_action

N1 = "esp32-000000000001"
N2 = "esp32-000000000002"
U = "user:vedant"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    def __init__(self, hub, fake_gateway=None, insecure=True):
        self.port = free_port()
        self.app = create_app(hub, mock_gateway=fake_gateway, insecure_no_auth=insecure)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.url, timeout=10)

    def stop(self):
        self.http.close()
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def api(home):
    s = Served(home.hub, home.gw)
    home.api = s
    yield s
    s.stop()


@pytest.fixture
def empty_api(env):
    s = Served(env.hub, env.gw)
    yield s
    s.stop()


def test_health_and_version(api):
    h = api.http.get("/health").json()
    assert h["ok"] and h["mqtt_connected"] is True and h["nodes"] == 2
    v = api.http.get("/registry/version").json()
    assert v["version"] >= 2 and "last_event_seq" in v


def test_full_provisioning_to_control_flow(empty_api, env):
    c = empty_api.http
    r = c.post("/admin/provision-sessions", json={"node_id": N1, "wifi": WIFI}, headers={"X-Actor": "admin1"})
    assert r.status_code == 201 and r.json()["bundle"]["mqtt"]["username"] == N1
    m = r.json()["bundle"]["mqtt"]
    from device_hub.mock import FakeNode
    node = FakeNode(make_announce(N1, [light_ep()]), env.broker.client(m["username"], m["password"], N1, FakeNode.will(N1)))
    assert node.start()
    env.broker.flush()
    nodes = c.get("/admin/nodes").json()
    assert nodes[0]["status"] == "pending" and nodes[0]["pending_endpoints"][0]["local_id"] == "l1"
    r = c.post(f"/admin/nodes/{N1}/approve", json={"endpoints": [{"local_id": "l1", "name": "Lamp", "room": "Study"}]})
    assert r.status_code == 200
    eid = r.json()[0]["endpoint_id"]
    r = c.post(f"/devices/{eid}/set", json={"capability": "brightness", "value": 66})
    assert r.status_code == 200 and r.json()["ok"] is True and node.values["l1"]["brightness"] == 66
    r = c.patch(f"/admin/endpoints/{eid}", json={"name": "Desk lamp"})
    assert r.json()["name"] == "Desk lamp" and r.json()["endpoint_id"] == eid
    assert c.get(f"/devices/{eid}").json()["name"] == "Desk lamp"
    assert c.post(f"/admin/nodes/{N1}/identify", json={"duration_s": 4}).status_code == 202
    # forget: 409 + usage first, then 204
    c.post("/scenes", json={"name": "S", "actions": [{"target": {"endpoint_id": eid}, "capability": "power", "value": True}]})
    r = c.delete(f"/admin/nodes/{N1}")
    assert r.status_code == 409 and r.json()["scenes"] == 1 and r.json()["message"] == "used by 1 scene, 0 automations"
    assert c.delete(f"/admin/nodes/{N1}?confirm=true").status_code == 204
    assert N1 not in env.broker.users
    assert c.get(f"/devices/{eid}").status_code == 404
    assert c.get("/scenes").json()[0]["broken"] is True


def test_error_mapping(api, home):
    c = api.http
    assert c.get("/devices/ep_00000000").status_code == 404
    assert c.post("/admin/nodes/esp32-000000000001/approve", content=b"{nope").status_code == 400
    assert c.post("/admin/nodes/esp32-000000000001/approve", json=[1]).status_code == 400
    assert c.post("/admin/nodes/bogus/identify", json={}).status_code == 400
    assert c.post(f"/devices/{home.ids['light']}/set", json={"capability": "power"}).status_code == 400
    assert c.post(f"/devices/{home.ids['light']}/set", json={"capability": "power", "value": True, "evil": 1}).status_code == 400
    r = c.post("/admin/provision-sessions", content=b"x" * 70000)
    assert r.status_code == 413
    assert c.get("/nope").status_code == 404
    assert c.post("/admin/endpoints/ep_00000000", json={}).status_code == 405


def test_set_with_confirmation_over_http(api, home):
    eid = home.ids["lock"]
    body = {"capability": "lock", "value": "unlocked", "user_id": U}
    assert api.http.post(f"/devices/{eid}/set", json=body).json()["error"] == "confirmation_required"
    a = api.http.post(f"/devices/{eid}/action", json={"capability": "lock", "value": "unlocked"}).json()
    assert a["risk"] == "confirm" and a["action"] == set_state_action(eid, "lock", "unlocked")
    tok = api.http.post("/mock/mint", json={"user_id": U, "action": a["action"]}).json()["confirmation_token"]
    r = api.http.post(f"/devices/{eid}/set", json={**body, "confirmation_token": tok})
    assert r.json()["ok"] is True


def test_scene_crud_and_run_status_codes(api, home):
    c = api.http
    acts = [{"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": True},
            {"target": {"endpoint_id": home.ids["lock"]}, "capability": "lock", "value": "locked"}]
    s = c.post("/scenes", json={"name": "Leave", "actions": acts, "owner": "user:v"}).json()
    sid = s["scene_id"]
    assert c.post("/scenes", json={"name": "leave", "actions": acts}).status_code == 409
    r = c.post(f"/scenes/{sid}/run", json={"user_id": U})
    assert r.status_code == 409 and r.json()["status"] == "needs_confirmation"
    info = c.get(f"/scenes/{sid}/confirmation").json()
    tok = c.post("/mock/mint", json={"user_id": U, "action": info["action"]}).json()["confirmation_token"]
    assert c.post("/mock/mint", json={"user_id": U, "action": info["action"]}).status_code == 200
    r = c.post(f"/scenes/{sid}/run", json={"user_id": U, "confirmation_token": tok})
    assert r.status_code == 200 and r.json()["executed"] is True
    assert c.post("/scenes/sc_00000000/run", json={}).status_code == 404
    assert c.put(f"/scenes/{sid}", json={"name": "Leave 2"}).json()["name"] == "Leave 2"
    assert c.delete(f"/scenes/{sid}").status_code == 204
    assert c.get(f"/scenes/{sid}").status_code == 404


def test_audit_and_rejections_endpoints(api, home):
    home.hub.admin.patch_endpoint(home.ids["light"], {"name": "X"}, "someone")
    a = api.http.get("/admin/audit?limit=5").json()
    assert a and a[0]["actor"] and "token" not in json.dumps(a).lower()
    assert api.http.get("/admin/audit?limit=abc").status_code == 400
    assert api.http.get("/admin/rejections").json() == []


def test_sse_registry_events_and_replay(api, home):
    got: list[dict] = []
    ready = threading.Event()

    def reader():
        with httpx.stream("GET", api.url + "/events", timeout=15) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                    if event == "hello":
                        ready.set()
                elif line.startswith("data:") and event != "hello":
                    got.append(json.loads(line.split(":", 1)[1]))
                    if len(got) >= 1:
                        return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    assert ready.wait(5)
    api.http.patch(f"/admin/endpoints/{home.ids['light']}", json={"name": "Streamed"})
    t.join(10)
    assert got and got[0]["type"] == "registry_changed" and home.ids["light"] in got[0]["data"]["endpoint_ids"]
    # replay from an old sequence number via Last-Event-ID
    seen = []
    with httpx.stream("GET", api.url + "/events", headers={"Last-Event-ID": "0"}, timeout=10) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                d = json.loads(line.split(":", 1)[1])
                if "type" in d:
                    seen.append(d["type"])
                if len(seen) >= 3:
                    break
    assert "registry_changed" in seen
    assert api.http.get("/events?types=bogus").status_code == 400


def test_mcp_over_streamable_http(api, home):
    async def go():
        async with streamablehttp_client(api.url + "/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                res = await s.call_tool("set_state", {"endpoint_id": home.ids["light"], "capability": "power", "value": True})
                lock = await s.call_tool("set_state", {"endpoint_id": home.ids["lock"], "capability": "lock", "value": "unlocked"},
                                         meta={"user_id": U})
                return [t.name for t in tools], json.loads(res.content[0].text), json.loads(lock.content[0].text)

    names, ok, locked = asyncio.run(go())
    assert len(names) == 5 and ok["ok"] is True and locked["error"] == "confirmation_required"


def test_mcp_rejects_foreign_host_header(api):
    r = httpx.post(api.url + "/mcp", headers={"Host": "evil.example", "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code in (400, 403, 421)


def _verifiers(tmp_path, agent=True):
    (tmp_path / "gateway.sha256").write_text(hashlib.sha256(b"gw-secret-value").hexdigest())
    if agent:
        (tmp_path / "agent.sha256").write_text(hashlib.sha256(b"agent-secret-value").hexdigest())
    return str(tmp_path)


def test_caller_auth_scopes_401_403(home, tmp_path):
    home.hub.cfg.auth.verifier_dir = _verifiers(tmp_path)
    s = Served(home.hub, home.gw, insecure=False)
    try:
        gw = {"X-Service-Secret": "gw-secret-value"}
        ag = {"X-Service-Secret": "agent-secret-value"}
        mcp = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert s.http.get("/health").status_code == 200                                   # the only open operation
        assert s.http.get("/admin/nodes").status_code == 401                              # no secret
        assert s.http.get("/admin/nodes", headers={"X-Service-Secret": "wrong"}).status_code == 401
        assert s.http.get("/admin/nodes", headers=gw).status_code == 200
        for method, path in (("GET", "/admin/nodes"), ("POST", "/admin/provision-sessions"), ("GET", "/devices"),
                             ("GET", "/scenes"), ("GET", "/events"), ("GET", "/registry/version"), ("GET", "/rooms"),
                             ("GET", "/admin/audit"), ("POST", "/mock/mint"), ("DELETE", "/admin/nodes/esp32-000000000001")):
            assert s.http.request(method, path, headers=ag, json={}).status_code == 403, (method, path)   # valid secret, wrong scope
        assert s.http.post("/mcp", headers={**ag, **mcp}, json=body).status_code == 200      # the agent's only operation
        assert s.http.get("/mcp", headers=ag).status_code == 403                             # POST only
        assert s.http.post("/mcp", headers={**gw, **mcp}, json=body).status_code == 200      # gateway scope includes /mcp
        assert s.http.post("/mcp", headers=mcp, json=body).status_code == 401
    finally:
        s.stop()


def test_refuses_to_start_without_caller_auth(home, tmp_path):
    home.hub.cfg.auth.verifier_dir = ""
    with pytest.raises(SystemExit):
        create_app(home.hub)                                       # no verifier dir, no flag
    with pytest.raises(SystemExit):
        create_app(home.hub, insecure_no_auth=True)                # the flag without a mock gateway (= without --mock)
    home.hub.cfg.auth.verifier_dir = str(tmp_path / "missing")
    with pytest.raises(SystemExit):
        create_app(home.hub)
    home.hub.cfg.auth.verifier_dir = _verifiers(tmp_path, agent=False)
    create_app(home.hub)                                           # gateway verifier alone is enough to start


def test_cli_insecure_flag_only_with_mock(capsys):
    from device_hub.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(["--insecure-no-auth"])
    assert e.value.code == 2 and "--mock" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--mock", "--port", "0"])                            # --mock alone still needs auth or the flag


def test_unknown_routes_are_json_404(api):
    r = api.http.get("/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
