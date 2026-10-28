"""gateway.yaml vs the running app: every route exists, auth requirement matches, response schemas validate."""
from __future__ import annotations

import pytest
import yaml
from jsonschema import Draft202012Validator
from starlette.routing import Mount, WebSocketRoute

from conftest import CONTRACTS
from calls import call

SPEC = yaml.safe_load((CONTRACTS / "openapi" / "gateway.yaml").read_text(encoding="utf-8"))
METHODS = ("get", "post", "put", "patch", "delete")
OPS = [(p, m, op) for p, item in SPEC["paths"].items() for m, op in item.items() if m in METHODS]
PUBLIC = [(p, m, op) for p, m, op in OPS if not p.startswith("/internal/")]
INTERNAL = [(p, m, op) for p, m, op in OPS if p.startswith("/internal/")]


def validate(schema: dict, instance) -> None:
    Draft202012Validator({**schema, "components": SPEC["components"]}).validate(instance)


def response_schema(op: dict, status: int):
    resp = op["responses"].get(str(status))
    if resp is None:
        return None
    if "$ref" in resp:
        resp = SPEC["components"]["responses"][resp["$ref"].split("/")[-1]]
    return ((resp.get("content") or {}).get("application/json") or {}).get("schema")


def effective_security(op: dict) -> list:
    return op.get("security", SPEC["security"])


def test_spec_version_known():
    assert SPEC["info"]["version"] == "2.13.0", "gateway.yaml changed: re-check the implementation, then update this pin"


def test_every_public_route_exists_and_no_extra(gw):
    have = set()
    flat = []
    for r in gw.app.routes:
        flat += list(r.original_router.routes) if hasattr(r, "original_router") else [r]
    for r in flat:
        if isinstance(r, Mount):
            continue
        if isinstance(r, WebSocketRoute):
            have.add((r.path, "get"))
        else:
            have |= {(r.path, m.lower()) for m in r.methods if m not in ("HEAD", "OPTIONS")}
    want = {(p, m) for p, m, _ in PUBLIC}
    assert want - have == set(), f"missing routes: {sorted(want - have)}"
    assert have - want == set(), f"routes not in gateway.yaml: {sorted(have - want)}"


@pytest.mark.parametrize("path,method,op", [x for x in PUBLIC if x[0] != "/ws"], ids=lambda x: x if isinstance(x, str) else "")
def test_auth_requirement(gw, path, method, op):
    sec = effective_security(op)
    url = path.replace("{endpoint_id}", "ep_00000001").replace("{node_id}", "esp32-aabbccddeeff").replace("{scene_id}", "sc_00000001") \
        .replace("{request_id}", "x").replace("{rule_id}", "ru_00000001").replace("{memory_id}", "m1").replace("{user_id}", "u1")
    r = gw.pub.request(method.upper(), url, json={})
    if sec == []:
        assert r.status_code != 401 or op["operationId"] in ("login", "refresh")      # public ops reject creds, not the missing header
        assert r.status_code != 403
    else:
        assert r.status_code == 401, f"{method} {path} accepted an unauthenticated call"
        assert r.json()["error"] == "unauthorized"
        r = gw.pub.request(method.upper(), url, json={}, headers={"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401
        if op.get("x-required-role") == "admin":
            assert gw.pub.request(method.upper(), url, json={}, headers=gw.h("member")).status_code == 403
            assert gw.pub.request(method.upper(), url, json={}, headers=gw.h("guest")).status_code == 403


def test_websocket_requires_a_valid_ticket(gw):
    from starlette.websockets import WebSocketDisconnect
    gw.ctx.cfg.ws_auth_timeout_s = 0.3
    with pytest.raises(WebSocketDisconnect) as e:
        with gw.pub.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "ticket": "A" * 43})
            ws.receive_json()
    assert e.value.code == 4401


def test_scene_target_is_validated_strictly(gw):
    """The contract now makes the two target shapes mutually exclusive (additionalProperties:false): no anyOf workaround."""
    schema = SPEC["components"]["schemas"]["SceneAction"]["properties"]["target"]
    assert "oneOf" in schema and "anyOf" not in schema
    ok = [{"endpoint_id": "ep_00000001"}, {"room": "Bedroom"}, {"type": "light", "tag": "night"}]
    bad = [{}, {"endpoint_id": "ep_00000001", "room": "Bedroom"}, {"endpoint_id": "ep_00000001", "x": 1}, {"bogus": 1},
           {"room": "A", "bogus": 1}, {"endpoint_id": "nope"}, {"type": "toaster"}, {"tag": "UPPER"}]
    sc = {"$ref": "#/components/schemas/SceneAction"}
    for t in ok:
        validate(sc, {"target": t, "capability": "power", "value": True})
        r = gw.pub.post("/scenes", json={"name": "t", "actions": [{"target": t, "capability": "power", "value": True}]},
                        headers=gw.h("admin"))
        assert r.status_code == 201, t
    for t in bad:
        with pytest.raises(Exception):
            validate(sc, {"target": t, "capability": "power", "value": True})
        r = gw.pub.post("/scenes", json={"name": "t", "actions": [{"target": t, "capability": "power", "value": True}]},
                        headers=gw.h("admin"))
        assert r.status_code == 400, (t, r.status_code)


def test_ws_security_is_none_and_uses_tickets():
    op = SPEC["paths"]["/ws"]["get"]
    assert op["security"] == [] and "/ws/ticket" in SPEC["paths"]


def test_every_operation_response_validates(gw):
    covered = set()
    for path, method, op in PUBLIC:
        oid = op["operationId"]
        if oid == "websocket":
            covered.add(oid)
            continue
        role = None if effective_security(op) == [] else ("admin" if op.get("x-required-role") == "admin" else "member")
        r, want = call(gw, oid, role)
        assert r.status_code == want, f"{oid}: HTTP {r.status_code} {r.text[:200]}"
        assert str(r.status_code) in op["responses"], f"{oid}: status {r.status_code} not declared in the contract"
        sch = response_schema(op, r.status_code)
        if sch is not None and r.content:
            validate(sch, r.json())
        elif oid == "chat":
            assert r.headers["content-type"].startswith("text/event-stream")
        covered.add(oid)
    assert covered == {op["operationId"] for _, _, op in PUBLIC}


def test_error_responses_match_error_schema(gw):
    err = SPEC["components"]["schemas"]["Error"]
    for r in (gw.pub.get("/devices"), gw.pub.post("/auth/login", json={"username": "admin", "password": "wrong"}),
              gw.pub.get("/admin/users", headers=gw.h("member")), gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": 1},
                                                                           headers=gw.h("guest")),
              gw.pub.post("/devices/ep_00000001/set", json={"bogus": 1}, headers=gw.h("admin")),
              gw.pub.get("/nope"), gw.pub.post("/internal/confirmations/consume", json={})):
        validate(err, r.json())


def test_mint_forbidden_and_declared_statuses(gw):
    op = SPEC["paths"]["/confirmations"]["post"]
    r = gw.pub.post("/confirmations", json={"action": {"type": "set_state", "endpoint_id": "ep_00000001", "capability": "power", "value": True}},
                    headers=gw.h("guest"))
    assert r.status_code == 403 and "403" in op["responses"]
    r = gw.pub.post("/confirmations", json={"request_id": "nope"}, headers=gw.h("member"))
    assert r.status_code == 404 and "404" in op["responses"]


@pytest.mark.parametrize("path,method,op", INTERNAL)
def test_internal_operation_is_internal_only(gw, path, method, op):
    assert op["security"] == [{"deviceHubSecret": []}]
    # not on the public listener, any method
    for m in ("GET", "POST", "PUT", "DELETE"):
        assert gw.pub.request(m, path, json={}).status_code == 404
        assert gw.pub.request(m, path, json={}, headers=gw.h("admin")).status_code == 404
    # on the internal listener: 401 without or with a wrong secret
    body = {"token": "A" * 43, "user_id": "u", "action_hash": "0" * 64}
    assert gw.int.post(path, json=body).status_code == 401
    assert gw.int.post(path, json=body, headers={"X-Service-Secret": "wrong"}).status_code == 401
    r = gw.int.post(path, json=body, headers=gw.secret_hdr())
    assert r.status_code == 403
    validate(response_schema(op, 403), r.json())


@pytest.mark.parametrize("kind,expect", [("valid", 201), ("invalid", 400)])
def test_scene_action_contract_samples(gw, kind, expect):
    import json
    files = sorted((CONTRACTS / "samples" / "scene-action" / kind).glob("*.json"))
    assert files
    for f in files:
        action = json.loads(f.read_text(encoding="utf-8"))
        r = gw.pub.post("/scenes", json={"name": "sample", "actions": [action]}, headers=gw.h("admin"))
        assert r.status_code == expect, f"{kind}/{f.name}: {r.status_code}"


HUB = yaml.safe_load((CONTRACTS / "openapi" / "device-hub-internal.yaml").read_text(encoding="utf-8"))


def hub_validate(instance) -> None:
    Draft202012Validator({"$ref": "#/components/schemas/SceneRunNotExecuted", "components": HUB["components"]}).validate(instance)


@pytest.mark.parametrize("kind", ["valid", "invalid"])
def test_scene_run_409_samples_and_what_the_gateway_forwards(gw, kind):
    import json
    files = sorted((CONTRACTS / "samples" / "scene-run-409" / kind).glob("*.json"))
    assert files
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if kind == "valid":
            hub_validate(data)
        else:
            with pytest.raises(Exception):
                hub_validate(data)
    # the gateway passes the hub's 409 through untouched, and it is exactly that schema
    r = gw.pub.post("/scenes/sc_00000002/run", headers=gw.h("admin"))
    assert r.status_code == 409
    hub_validate(r.json())
    assert r.json()["status"] == "needs_confirmation" and r.json()["scene_id"] == "sc_00000002" and len(r.json()["action_hash"]) == 64
