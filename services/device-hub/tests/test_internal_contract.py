"""The HTTP API must implement contracts/openapi/device-hub-internal.yaml exactly: same operations, same scopes and
auth behaviour (401 unknown, 403 wrong scope, /health open), responses valid against the contract's schemas."""
from __future__ import annotations

import hashlib
import re

import jsonschema
import pytest
import yaml

from conftest import CONTRACTS
from test_api import Served

SPEC = yaml.safe_load((CONTRACTS / "openapi" / "device-hub-internal.yaml").read_text(encoding="utf-8"))
METHODS = ("get", "post", "put", "patch", "delete")
OPS = [(m.upper(), p, op) for p, item in SPEC["paths"].items() for m, op in item.items() if m in METHODS]
GW = {"X-Service-Secret": "gw-secret-value"}
AG = {"X-Service-Secret": "agent-secret-value"}
PLACEHOLDER = {"node_id": "esp32-00000000ffff", "endpoint_id": "ep_ffffffff", "scene_id": "sc_ffffffff"}


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: PLACEHOLDER[m.group(1)], path)


def schema(name: str):
    return jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]})


@pytest.fixture
def served(home, tmp_path):
    (tmp_path / "gateway.sha256").write_text(hashlib.sha256(b"gw-secret-value").hexdigest())
    (tmp_path / "agent.sha256").write_text(hashlib.sha256(b"agent-secret-value").hexdigest())
    home.hub.cfg.auth.verifier_dir = str(tmp_path)
    s = Served(home.hub, home.gw, insecure=False)       # gw given so /mock/mint exists, auth still enforced
    yield s
    s.stop()


def test_operation_sets_are_identical(served):
    mine = set()
    for r in served.app.app.routes:
        for m in r.methods or ():
            if m != "HEAD":
                mine.add((m, r.path))
    mine.add(("POST", "/mcp"))                          # mounted ahead of the router by the gate
    spec = {(m, p) for m, p, _ in OPS}
    assert mine == spec, f"only in code: {sorted(mine - spec)}; only in contract: {sorted(spec - mine)}"


@pytest.mark.parametrize("method,path,op", OPS, ids=lambda x: x if isinstance(x, str) else "")
def test_scopes_match_x_scopes(served, method, path, op):
    scopes = op["x-scopes"]
    url = concrete(path)
    kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
    if path == "/events":
        return                                       # streaming; scopes checked in test_agent_events.py
    no_secret = served.http.request(method, url, **kw)
    if scopes == ["open"]:
        assert no_secret.status_code == 200
        return
    assert no_secret.status_code == 401, "missing secret must be 401"
    assert served.http.request(method, url, headers={"X-Service-Secret": "nope"}, **kw).status_code == 401
    agent = served.http.request(method, url, headers=AG, **kw)
    if "agent" in scopes:
        assert agent.status_code not in (401, 403)
    else:
        assert agent.status_code == 403, f"{method} {path}: agent scope must be refused"
    gw = served.http.request(method, url, headers=GW, **kw)
    assert gw.status_code not in (401, 403), f"{method} {path}: gateway scope must reach every operation"


def test_the_agent_scope_is_exactly_two_operations(served):
    assert sorted((m, p) for m, p, op in OPS if "agent" in op["x-scopes"]) == [("GET", "/events"), ("POST", "/mcp")]
    assert sorted(SPEC["paths"]["/mcp"]["post"]["x-mcp-tools"]) == sorted(
        ["list_devices", "get_state", "set_state", "run_scene", "read_sensor"])


def test_responses_validate_against_contract_schemas(served, home):
    c, h = served.http, GW
    ok = lambda name, doc: schema(name).validate(doc)  # noqa: E731
    for n in c.get("/admin/nodes", headers=h).json():
        ok("Node", n)
    for e in c.get("/devices", headers=h).json():
        ok("Endpoint", e)
    ok("Endpoint", c.get(f"/devices/{home.ids['light']}", headers=h).json())
    ok("Health", c.get("/health").json())
    r = c.post(f"/devices/{home.ids['light']}/set", headers=h, json={"capability": "power", "value": True})
    ok("SetResult", r.json())
    assert r.json()["status"] == "done"
    r = c.patch(f"/admin/endpoints/{home.ids['light']}", headers=h, json={"tags": ["night"]})
    ok("Endpoint", r.json())
    assert r.json()["tags"] == ["night"]
    s = c.post("/scenes", headers=h, json={"name": "X", "owner": "u", "actions": [
        {"target": {"tag": "night"}, "capability": "power", "value": False}]}).json()
    ok("Scene", s)
    ok("Scene", c.get(f"/scenes/{s['scene_id']}", headers=h).json())
    r = c.delete(f"/admin/nodes/esp32-000000000001", headers=h)
    assert r.status_code == 409
    ok("ForgetUsage", r.json())
    r = c.post("/admin/nodes/esp32-000000000001/identify", headers=h, json={"duration_s": 2})
    assert r.status_code == 202
    ok("SentAcked", r.json())
    for a in c.get("/admin/audit?limit=5", headers=h).json():
        assert isinstance(a["ts"], int) and isinstance(a["detail"], dict)
    assert c.get("/registry/version", headers=h).json().keys() >= {"version", "last_event_seq"}
    assert all(isinstance(x, str) for x in c.get("/rooms", headers=h).json())
    act = c.post(f"/devices/{home.ids['lock']}/action", headers=h, json={"capability": "lock", "value": "locked"}).json()
    assert re.fullmatch(r"[0-9a-f]{64}", act["action_hash"]) and act["risk"] == "confirm"
    conf = c.get(f"/scenes/{s['scene_id']}/confirmation", headers=h).json()
    assert re.fullmatch(r"[0-9a-f]{64}", conf["action_hash"])


def test_mock_mint_is_404_without_mock(home, tmp_path):
    (tmp_path / "gateway.sha256").write_text(hashlib.sha256(b"gw-secret-value").hexdigest())
    home.hub.cfg.auth.verifier_dir = str(tmp_path)
    s = Served(home.hub, None, insecure=False)
    try:
        assert s.http.post("/mock/mint", headers=GW, json={"user_id": "u", "action": {}}).status_code == 404
    finally:
        s.stop()
