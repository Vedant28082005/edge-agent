"""Integration against the REAL device-hub (worktree ..\\edge-agent-s1, branch s1-device-hub), run from there in mock style
(in-memory broker + scripted fake nodes: no Docker, no real MQTT broker). The gateway is real too: two uvicorn listeners on free
ports, the hub client reads the gateway secret from a file, and the hub consumes tokens through the gateway's REAL internal listener.

Skipped (not passed) when the S1 worktree or its venv is missing."""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import REPO
from gateway.app import create_public_app
from gateway.config import Config
from gateway.context import build_ctx
from gateway.internal import create_internal_app
from gateway.mock import FakeAgent, asgi
from gateway.security import SECRETS, hash_password, sha256_hex
from test_listeners import Srv, free_port

S1 = REPO.parent / "edge-agent-s1" / "services" / "device-hub"
S1_PY = S1 / ".venv" / "Scripts" / "python.exe"
if not S1_PY.exists():
    S1_PY = S1 / ".venv" / "bin" / "python"
pytestmark = pytest.mark.skipif(not S1_PY.exists(), reason="S1 device-hub worktree/venv not found")


class Stack:
    pass


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("it")
    st = Stack()
    st.tmp = tmp
    st.gw_secret, st.dh_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    for s in (st.gw_secret, st.dh_secret):
        SECRETS.add(s)
    (tmp / "gateway.secret").write_text(st.gw_secret)
    (tmp / "device-hub.secret").write_text(st.dh_secret)
    (tmp / "gw-verifiers").mkdir()
    (tmp / "gw-verifiers" / "device-hub.sha256").write_text(sha256_hex(st.dh_secret))
    (tmp / "hub-verifiers").mkdir()
    (tmp / "hub-verifiers" / "gateway.sha256").write_text(sha256_hex(st.gw_secret))

    hub_port, int_port, pub_port = free_port(), free_port(), free_port()
    cfg = Config(data_dir=str(tmp / "data"), hub_secret_file=str(tmp / "gateway.secret"), verifier_dir=str(tmp / "gw-verifiers"))
    cfg.hub.url = f"http://127.0.0.1:{hub_port}"
    st.agent = FakeAgent()
    st.ctx = build_ctx(cfg, transports={"agent": asgi(st.agent.app)})
    st.ctx.autostart = True
    st.pw = {}
    for name, role, rooms in (("admin", "admin", []), ("member", "member", ["Bedroom"]), ("guest", "guest", ["Bedroom"])):
        st.pw[name] = secrets.token_urlsafe(12)
        SECRETS.add(st.pw[name])
        st.ctx.auth.create(name, hash_password(st.pw[name]), role, rooms)

    buf = io.StringIO()                                       # gateway logs at DEBUG, raw handler
    st.gw_log = logging.StreamHandler(buf)
    st.gw_log_buf = buf
    logging.getLogger().addHandler(st.gw_log)
    logging.getLogger().setLevel(logging.DEBUG)

    internal = Srv(create_internal_app(st.ctx), int_port)
    pub = Srv(create_public_app(st.ctx), pub_port)
    errlog = open(tmp / "hub.stderr.log", "wb")
    proc = subprocess.Popen(
        [str(S1_PY), str(Path(__file__).with_name("hub_harness.py")), "--port", str(hub_port),
         "--gateway-url", f"http://127.0.0.1:{int_port}", "--device-hub-secret-file", str(tmp / "device-hub.secret"),
         "--verifier-dir", str(tmp / "hub-verifiers"), "--hub-dir", str(S1)],
        cwd=str(S1), stdout=subprocess.PIPE, stderr=errlog)
    st.proc, st.errlog = proc, errlog
    try:
        line = proc.stdout.readline().decode().strip()
        assert line == "READY", f"hub harness did not start: {(tmp / 'hub.stderr.log').read_text()[-800:]}"
        internal.__enter__()
        pub.__enter__()
        st.url = pub.url
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{hub_port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        st.hub_url = f"http://127.0.0.1:{hub_port}"
        yield st
    finally:
        pub.__exit__()
        internal.__exit__()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()                                       # our own PID only
        errlog.close()
        logging.getLogger().removeHandler(st.gw_log)


@pytest.fixture(scope="module")
def api(stack):
    c = httpx.Client(base_url=stack.url, timeout=15)
    hdr = {}
    for n in stack.pw:
        r = c.post("/auth/login", json={"username": n, "password": stack.pw[n]})
        assert r.status_code == 200, r.text
        hdr[n] = {"Authorization": "Bearer " + r.json()["access_token"]}
    c.hdr = hdr
    yield c
    c.close()


def by_name(api, who, name):
    return next(e for e in api.get("/devices", headers=api.hdr[who]).json() if e["name"] == name)


def test_hub_requires_the_gateway_secret(stack):
    assert httpx.get(stack.hub_url + "/devices").status_code == 401
    assert httpx.get(stack.hub_url + "/devices", headers={"X-Service-Secret": "nope"}).status_code == 401
    assert httpx.get(stack.hub_url + "/devices", headers={"X-Service-Secret": stack.gw_secret}).status_code == 200


def test_list_devices_through_the_gateway_with_room_filtering(api):
    admin = api.get("/devices", headers=api.hdr["admin"])
    assert admin.status_code == 200
    names = {e["name"]: e for e in admin.json()}
    assert {"Ceiling light", "Fan", "Front door"} <= set(names) and names["Front door"]["risk"] == "confirm"
    member = {e["name"] for e in api.get("/devices", headers=api.hdr["member"]).json()}
    assert "Ceiling light" in member and "Front door" not in member                      # Hall is not the member's room
    assert api.get("/devices?room=Bedroom", headers=api.hdr["guest"]).status_code == 200


def test_approve_a_node_end_to_end(api):
    nodes = api.get("/admin/nodes", headers=api.hdr["admin"]).json()
    pending = [n for n in nodes if n["node_id"] == "esp32-aabbccddee09"]
    assert pending, nodes
    assert api.get("/admin/nodes", headers=api.hdr["member"]).status_code == 403
    body = {"endpoints": [{"local_id": "l9", "name": "Reading lamp", "room": "Bedroom", "aliases": ["lamp"], "tags": ["night"]}]}
    assert api.post("/admin/nodes/esp32-aabbccddee09/approve", json=body, headers=api.hdr["member"]).status_code == 403
    r = api.post("/admin/nodes/esp32-aabbccddee09/approve", json=body, headers=api.hdr["admin"])
    assert r.status_code == 200, r.text
    ep = r.json()[0]
    assert ep["endpoint_id"].startswith("ep_") and ep["name"] == "Reading lamp"
    assert by_name(api, "member", "Reading lamp")["endpoint_id"] == ep["endpoint_id"]      # visible to the member of that room
    dup = api.post("/admin/nodes/esp32-aabbccddee09/approve", json=body, headers=api.hdr["admin"])
    assert dup.status_code in (409, 404)                                                    # hub rules pass through


def test_set_state_through_the_gateway(api):
    lamp = by_name(api, "member", "Ceiling light")["endpoint_id"]
    r = api.post(f"/devices/{lamp}/set", json={"capability": "power", "value": True}, headers=api.hdr["member"])
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"] == "done", r.text
    assert api.post(f"/devices/{lamp}/set", json={"capability": "power", "value": True}, headers=api.hdr["guest"]).status_code == 403
    assert api.post(f"/devices/{lamp}/set", json={"capability": "brightness", "value": 500}, headers=api.hdr["admin"]).json()["ok"] is False
    assert api.post("/devices/ep_ffffffff/set", json={"capability": "power", "value": True}, headers=api.hdr["admin"]).status_code == 404
    time.sleep(0.2)
    assert by_name(api, "admin", "Ceiling light")["state"].get("power") is True


def test_risk_confirm_set_state_full_token_flow(api, stack):
    door = by_name(api, "admin", "Front door")["endpoint_id"]
    unlock = {"capability": "lock", "value": "unlocked"}
    a, m = api.hdr["admin"], api.hdr["member"]
    # 1. blocked without a token: the hub executes nothing
    r = api.post(f"/devices/{door}/set", json=unlock, headers=a)
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    # 2. the member has no access to the Hall: no token for them
    action = {"type": "set_state", "endpoint_id": door, **unlock}
    assert api.post("/confirmations", json={"action": action}, headers=m).status_code == 403
    # 3. admin approves: the gateway mints exactly for the HUB's hash
    mint = api.post("/confirmations", json={"action": action}, headers=a)
    assert mint.status_code == 200 and mint.json()["status"] == "minted"
    tok = mint.json()["confirmation_token"]
    hub_hash = httpx.post(f"{stack.hub_url}/devices/{door}/action", json=unlock, headers={"X-Service-Secret": stack.gw_secret}).json()["action_hash"]
    assert mint.json()["action_hash"] == hub_hash
    # 4. a different action with that token is refused AND burns it (fail closed); a fresh token then works
    bad = api.post(f"/devices/{door}/set", json={"capability": "lock", "value": "locked", "confirmation_token": tok}, headers=a)
    assert bad.status_code == 409
    tok = api.post("/confirmations", json={"action": action}, headers=a).json()["confirmation_token"]
    ok = api.post(f"/devices/{door}/set", json={**unlock, "confirmation_token": tok}, headers=a)
    assert ok.status_code == 200 and ok.json()["ok"] is True, ok.text
    time.sleep(0.2)
    assert by_name(api, "admin", "Front door")["state"].get("lock") == "unlocked"
    # 5. replay is dead
    assert api.post(f"/devices/{door}/set", json={**unlock, "confirmation_token": tok}, headers=a).status_code == 409
    row = stack.ctx.db.all("SELECT result FROM audit_log WHERE action='confirm.consume' ORDER BY id")
    assert {"granted", "hash_mismatch", "used"} <= {r["result"] for r in row}


def test_wrong_user_cannot_spend_a_token_and_does_not_burn_it(api, stack):
    door = by_name(api, "admin", "Front door")["endpoint_id"]
    api.post(f"/devices/{door}/set", json={"capability": "lock", "value": "locked"}, headers=api.hdr["admin"])    # re-lock (no token needed)
    action = {"type": "set_state", "endpoint_id": door, "capability": "lock", "value": "unlocked"}
    tok = api.post("/confirmations", json={"action": action}, headers=api.hdr["admin"]).json()["confirmation_token"]
    member_id = stack.ctx.db.one("SELECT user_id FROM users WHERE username='member'")["user_id"]
    r = httpx.post(f"{stack.hub_url}/devices/{door}/set", headers={"X-Service-Secret": stack.gw_secret},
                   json={"capability": "lock", "value": "unlocked", "user_id": member_id, "confirmation_token": tok})
    assert r.json()["ok"] is False and r.json()["error"] == "confirmation_required"
    ok = api.post(f"/devices/{door}/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok}, headers=api.hdr["admin"])
    assert ok.status_code == 200 and ok.json()["ok"] is True                 # the owner's approval survived the foreign attempt


def test_risk_confirm_scene_full_token_flow(api, stack):
    a = api.hdr["admin"]
    light = by_name(api, "admin", "Ceiling light")["endpoint_id"]
    door = by_name(api, "admin", "Front door")["endpoint_id"]
    api.post(f"/devices/{door}/set", json={"capability": "lock", "value": "locked"}, headers=a)
    sc = api.post("/scenes", json={"name": "Welcome home", "actions": [
        {"target": {"endpoint_id": door}, "capability": "lock", "value": "unlocked"},
        {"target": {"endpoint_id": light}, "capability": "power", "value": False}]}, headers=a)
    assert sc.status_code == 201, sc.text
    sid = sc.json()["scene_id"]
    # hub-created scene shape reaches the client untouched, with the creator as owner
    assert sc.json()["owner"] == stack.ctx.db.one("SELECT user_id FROM users WHERE username='admin'")["user_id"]
    run = api.post(f"/scenes/{sid}/run", headers=a)
    assert run.status_code == 409 and run.json()["executed"] is False and run.json()["status"] == "needs_confirmation"
    mint = api.post("/confirmations", json={"action": {"type": "run_scene", "scene": sid}}, headers=a)
    assert mint.status_code == 200, mint.text
    tok = mint.json()["confirmation_token"]
    ok = api.post(f"/scenes/{sid}/run", json={"confirmation_token": tok}, headers=a)
    assert ok.status_code == 200 and ok.json()["executed"] is True, ok.text
    assert api.post(f"/scenes/{sid}/run", json={"confirmation_token": tok}, headers=a).status_code == 409        # single use
    # members: out of room -> 403 here (the hub applies no roles); guests assigned a risky scene still cannot confirm
    assert api.post(f"/scenes/{sid}/run", headers=api.hdr["member"]).status_code == 403
    gid = stack.ctx.db.one("SELECT user_id FROM users WHERE username='guest'")["user_id"]
    stack.ctx.db.run("INSERT OR IGNORE INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gid, sid))
    g = api.post(f"/scenes/{sid}/run", headers=api.hdr["guest"])
    assert g.status_code == 409 and g.json()["executed"] is False
    assert api.post("/confirmations", json={"action": {"type": "run_scene", "scene": sid}}, headers=api.hdr["guest"]).status_code == 403


def test_agent_flow_against_the_real_hub(api, stack):
    a = api.hdr["admin"]
    door = by_name(api, "admin", "Front door")["endpoint_id"]
    api.post(f"/devices/{door}/set", json={"capability": "lock", "value": "locked"}, headers=a)
    uid = stack.ctx.db.one("SELECT user_id FROM users WHERE username='admin'")["user_id"]
    act = {"type": "set_state", "endpoint_id": door, "capability": "lock", "value": "unlocked"}
    h = httpx.post(f"{stack.hub_url}/devices/{door}/action", json={"capability": "lock", "value": "unlocked"},
                   headers={"X-Service-Secret": stack.gw_secret}).json()["action_hash"]
    stack.ctx.confirm.register_request(uid, "s1", {"request_id": "req-it-1", "summary": "Unlock?", "action": act, "action_hash": h,
                                                   "expires_in_s": 60})
    r = api.post("/confirmations", json={"request_id": "req-it-1"}, headers=a)
    assert r.status_code == 200 and r.json()["status"] == "delivered_to_agent" and "confirmation_token" not in r.json()
    tok = stack.agent.deliveries[-1]["confirmation_token"]
    # the agent executor re-issues the call with the token out of band (hub HTTP path, user_id as supplied)
    res = httpx.post(f"{stack.hub_url}/devices/{door}/set", headers={"X-Service-Secret": stack.gw_secret},
                     json={"capability": "lock", "value": "unlocked", "user_id": uid, "confirmation_token": tok}).json()
    assert res["ok"] is True
    assert httpx.post(f"{stack.hub_url}/devices/{door}/set", headers={"X-Service-Secret": stack.gw_secret},
                      json={"capability": "lock", "value": "unlocked", "user_id": uid, "confirmation_token": tok}).json()["ok"] is False


def test_provision_session_admin_route_and_profile_resolution(api, stack):
    stack.ctx.profiles.put("home", "HomeNet", "profile-pass-IT1")
    r = api.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddee77", "wifi_profile": "home"}, headers=api.hdr["admin"])
    assert r.status_code == 201, r.text
    b = r.json()["bundle"]
    assert b["wifi"] == {"ssid": "HomeNet", "password": "profile-pass-IT1"} and b["mqtt"]["username"] == "esp32-aabbccddee77"
    SECRETS.add(b["mqtt"]["password"])
    stack.mqtt_pw = b["mqtt"]["password"]
    assert api.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddee77", "wifi_profile": "home"},
                    headers=api.hdr["member"]).status_code == 403


def test_admin_patch_forget_identify_map_to_the_hub(api):
    a = api.hdr["admin"]
    fan = by_name(api, "admin", "Fan")["endpoint_id"]
    r = api.patch(f"/admin/endpoints/{fan}", json={"name": "Desk fan", "tags": ["summer"]}, headers=a)
    assert r.status_code == 200 and r.json()["name"] == "Desk fan" and r.json()["tags"] == ["summer"]
    assert api.patch(f"/admin/endpoints/{fan}", json={"bogus": 1}, headers=a).status_code == 400
    assert api.patch("/admin/endpoints/ep_ffffffff", json={"name": "x"}, headers=a).status_code == 404
    nodes = api.get("/admin/nodes", headers=a).json()
    nid = next(n["node_id"] for n in nodes if n["node_id"] != "esp32-aabbccddee09" and n.get("status") == "online")
    assert api.post(f"/admin/nodes/{nid}/identify", json={"duration_s": 3}, headers=a).status_code in (202, 409)
    first = api.delete("/admin/nodes/esp32-aabbccddee09", headers=a)
    assert first.status_code in (204, 409)                                  # 409 = usage list when something depends on it
    if first.status_code == 409:
        assert api.delete("/admin/nodes/esp32-aabbccddee09?confirm=true", headers=a).status_code == 204


def test_secrets_never_reach_any_log(stack, api):
    time.sleep(0.3)
    hub_log = (stack.tmp / "hub.stderr.log").read_text(errors="replace")
    gw_log = stack.gw_log_buf.getvalue()
    toks = [d["confirmation_token"] for d in stack.agent.deliveries]
    leaks = [stack.gw_secret, stack.dh_secret, getattr(stack, "mqtt_pw", "x" * 20), "profile-pass-IT1", *toks, *stack.pw.values()]
    assert len(hub_log) > 200 and len(gw_log) > 200
    for s in leaks:
        assert s not in hub_log, "secret in the hub log"
        assert s not in gw_log, "secret in the gateway log"
    # no JWT or refresh token either
    for n in stack.pw:
        assert api.hdr[n]["Authorization"].split()[1] not in gw_log + hub_log


def test_websocket_receives_real_hub_state_filtered_by_role(stack, api):
    websockets = pytest.importorskip("websockets.sync.client")
    url = stack.url.replace("http", "ws") + "/ws"
    lamp = by_name(api, "admin", "Ceiling light")["endpoint_id"]
    door = by_name(api, "admin", "Front door")["endpoint_id"]
    socks = {}
    for who in ("admin", "member"):
        s = websockets.connect(url)
        s.send(json.dumps({"type": "auth", "token": api.hdr[who]["Authorization"].split()[1]}))
        assert json.loads(s.recv(5))["kind"] == "ws_ready"
        socks[who] = s
    time.sleep(1.5)                                                           # let the hub subscriber (re)connect
    cur = by_name(api, "admin", "Ceiling light")["state"].get("power")
    api.post(f"/devices/{lamp}/set", json={"capability": "power", "value": not cur}, headers=api.hdr["admin"])    # a real change
    dstate = by_name(api, "admin", "Front door")["state"].get("lock")
    want = "unlocked" if dstate == "locked" else "locked"
    tok = api.post("/confirmations", json={"action": {"type": "set_state", "endpoint_id": door, "capability": "lock", "value": want}},
                   headers=api.hdr["admin"]).json()["confirmation_token"]
    assert api.post(f"/devices/{door}/set", json={"capability": "lock", "value": want, "confirmation_token": tok},
                    headers=api.hdr["admin"]).status_code == 200

    def collect(s, secs=4.0):
        out, end = [], time.time() + secs
        while time.time() < end:
            try:
                out.append(json.loads(s.recv(max(0.1, end - time.time()))))
            except TimeoutError:
                break
        return out
    adm, mem = collect(socks["admin"]), collect(socks["member"])
    for s in socks.values():
        s.close()
    adm_eps = {m.get("endpoint_id") for m in adm if m["type"] == "state"}
    mem_eps = {m.get("endpoint_id") for m in mem if m["type"] == "state"}
    assert lamp in adm_eps and lamp in mem_eps, (adm, mem)
    assert door in adm_eps and door not in mem_eps                             # Hall events never reach the Bedroom member
    assert not any(m.get("kind") == "node_status" for m in mem)
