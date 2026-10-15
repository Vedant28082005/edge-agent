"""gateway.yaml 2.11.0: action log, WsMessage/SseEvent conformance of what the gateway really emits, capability settle_s,
the 409 with a server-built summary + action + hash, and the chat scope sent to the agent."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from jsonschema import Draft202012Validator

from conftest import CONTRACTS
from gateway.events import Event, from_agent, from_hub, note_settle, refresh_rooms
from gateway.mock import action_hash
from gateway.upstream import UpstreamError
from test_contract import SPEC, validate
from test_ws import connect, drain

LOCK = {"type": "set_state", "endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}
NODE = "esp32-aabbccddeeff"


def schema(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]})


def valid(name: str, instance) -> bool:
    return schema(name).is_valid(instance)


def run(coro):
    return asyncio.run(coro)


def sse_frames(text: str) -> list[dict]:
    """Parse an SSE body into the `{event, data}` objects SseEvent describes."""
    out = []
    for block in text.split("\n\n"):
        ev, data = None, []
        for ln in block.split("\n"):
            if ln.startswith("event:"):
                ev = ln[6:].strip()
            elif ln.startswith("data:"):
                data.append(ln[5:].lstrip(" "))
        if ev:
            out.append({"event": ev, "data": json.loads("\n".join(data))})
    return out


# ------------------------------------------------------------------ the contract's own samples (guards the validators)
@pytest.mark.parametrize("kind,name", [("ws-message", "WsMessage"), ("sse-event", "SseEvent"), ("action-log-page", "ActionLogPage"),
                                       ("confirmation-required", "ConfirmationRequired")])
def test_validators_agree_with_the_contract_samples(kind, name):
    base = CONTRACTS / "samples" / kind
    if not base.is_dir():
        pytest.skip("samples not present in this contracts checkout")
    for f in sorted((base / "valid").glob("*.json")):
        assert valid(name, json.loads(f.read_text(encoding="utf-8"))), f.name
    for f in sorted((base / "invalid").glob("*.json")):
        assert not valid(name, json.loads(f.read_text(encoding="utf-8"))), f.name


# ------------------------------------------------------------------ 3. settle_s on Capability
def test_settle_s_is_present_only_on_motion_capabilities(gw):
    for who in ("admin", "member", "guest"):
        r = gw.pub.get("/devices", headers=gw.h(who))
        assert r.status_code == 200
        for ep in r.json():
            validate({"$ref": "#/components/schemas/Endpoint"}, ep)
        blinds = next(e for e in r.json() if e["endpoint_id"] == "ep_00000006")
        assert blinds["capabilities"][0]["settle_s"] == 30
        lamp = next(e for e in r.json() if e["endpoint_id"] == "ep_00000001")
        assert "settle_s" not in lamp["capabilities"][0]


def test_bogus_settle_s_from_upstream_is_dropped(gw):
    hub = gw.m.hub
    hub.devices["ep_00000001"]["capabilities"] = [{"id": "power", "kind": "actuator", "type": "bool", "settle_s": 5}]   # ok: kept
    hub.devices["ep_00000002"]["capabilities"] = [{"id": "temp", "kind": "sensor", "type": "float", "settle_s": 5}]      # sensor
    hub.devices["ep_00000004"]["capabilities"] = [{"id": "power", "kind": "actuator", "type": "bool", "settle_s": 9999}]  # out of range
    hub.devices["ep_00000006"]["capabilities"] = [{"id": "position", "kind": "actuator", "type": "int", "settle_s": True}]   # bool
    got = {e["endpoint_id"]: e["capabilities"][0] for e in gw.pub.get("/devices", headers=gw.h("admin")).json()}
    assert got["ep_00000001"]["settle_s"] == 5
    assert all("settle_s" not in got[k] for k in ("ep_00000002", "ep_00000004", "ep_00000006"))


def test_set_on_a_motion_capability_answers_moving(gw):
    r = gw.pub.post("/devices/ep_00000006/set", json={"capability": "position", "value": 80}, headers=gw.h("member"))
    assert r.status_code == 200 and r.json()["status"] == "moving" and r.json()["verified"] is False
    validate({"$ref": "#/components/schemas/SetResult"}, r.json())


# ------------------------------------------------------------------ 4. 409 with summary + action + hash
def test_direct_set_on_a_confirm_endpoint_returns_the_server_built_409(gw):
    r = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked"}, headers=gw.h("admin"))
    assert r.status_code == 409
    b = r.json()
    assert schema("ConfirmationRequired").is_valid(b), b
    assert b["error"] == "confirmation_required" and b["summary"] == "Unlock Front door?"
    assert b["action"] == LOCK and b["action_hash"] == action_hash(LOCK) and b["expires_in_s"] == 60
    # the action in the body is exactly what POST /confirmations wants, and the hash is the one it mints for
    m = gw.pub.post("/confirmations", json={"action": b["action"]}, headers=gw.h("admin"))
    assert m.status_code == 200 and m.json()["action_hash"] == b["action_hash"]
    ok = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked",
                                                       "confirmation_token": m.json()["confirmation_token"]}, headers=gw.h("admin"))
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_direct_set_409_has_no_client_wording_and_other_409s_keep_the_error_shape(gw):
    gw.m.hub.devices["ep_00000003"]["name"] = "Front\x00 door\n<b>x</b>" + "y" * 100               # admin-set label: sanitized, bounded
    b = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "locked"}, headers=gw.h("admin")).json()
    assert schema("ConfirmationRequired").is_valid(b) and "\x00" not in b["summary"] and "\n" not in b["summary"] and len(b["summary"]) <= 200
    assert b["summary"].startswith("Lock Front")
    gw.m.hub.devices["ep_00000001"]["online"] = False
    r = gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": True}, headers=gw.h("admin"))
    assert r.status_code == 409 and r.json()["error"] == "endpoint_offline" and "summary" not in r.json()
    assert schema("Error").is_valid(r.json())


def test_direct_set_409_fails_closed_when_the_hash_cannot_be_built(gw):
    async def boom(*a, **k):
        raise UpstreamError("device-hub")
    orig = gw.ctx.hub.hub

    async def flaky(method, path, *a, **k):
        if path.endswith("/action"):
            return await boom()
        return await orig(method, path, *a, **k)
    gw.ctx.hub.hub = flaky
    r = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked"}, headers=gw.h("admin"))
    assert r.status_code in (502, 504) and "action_hash" not in r.text
    assert gw.m.hub.devices["ep_00000003"]["state"]["lock"] == "locked"


def test_scene_run_409_has_the_same_summary_and_hash_as_the_confirmation_flow(gw):
    r = gw.pub.post("/scenes/sc_00000002/run", headers=gw.h("admin"))
    assert r.status_code == 409
    b = r.json()
    validate({"$ref": "#/components/schemas/SceneRunResult"}, b)
    assert b["status"] == "needs_confirmation" and b["executed"] is False and b["ok"] is False
    action = {"type": "run_scene", "scene_id": "sc_00000002", "actions": b["needs_confirmation"]}
    assert b["action_hash"] == action_hash(action)
    assert "Open up" in b["summary"] and "Front door" in b["summary"] and len(b["summary"]) <= 200
    # the same summary + hash that the agent flow / direct mint use for this scene
    m = gw.pub.post("/confirmations", json={"action": {"type": "run_scene", "scene": "sc_00000002"}}, headers=gw.h("admin"))
    assert m.json()["action_hash"] == b["action_hash"]
    canon, h, summary = run(gw.ctx.confirm.describe(gw.ctx.auth.get(gw.uid("admin")), {"type": "run_scene", "scene_id": "sc_00000002"}))
    assert (h, summary, canon) == (b["action_hash"], b["summary"], action)
    ok = gw.pub.post("/scenes/sc_00000002/run", json={"confirmation_token": m.json()["confirmation_token"]}, headers=gw.h("admin"))
    assert ok.status_code == 200 and ok.json()["executed"] is True


def test_guest_scene_run_409_also_carries_summary_and_hash(gw):
    gw.ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gw.uid("guest"), "sc_00000002"))
    r = gw.pub.post("/scenes/sc_00000002/run", headers=gw.h("guest"))
    assert r.status_code == 409 and r.json()["summary"] and len(r.json()["action_hash"]) == 64


# ------------------------------------------------------------------ 5. chat scope
def last_chat(gw, who, text="hi"):
    r = gw.pub.post("/chat", json={"text": text}, headers=gw.h(who))
    return r, (gw.m.agent.requests[-1] if gw.m.agent.requests else None)


def test_admin_gets_explicit_null_scope(gw):
    r, req = last_chat(gw, "admin")
    assert r.status_code == 200
    assert "allowed_rooms" in req and "allowed_scenes" in req and req["allowed_rooms"] is None and req["allowed_scenes"] is None


def test_member_gets_lists_never_null(gw):
    gw.m.hub.add_scene("sc_00000009", "Mine", gw.uid("member"), [{"target": {"endpoint_id": "ep_00000003"}, "capability": "lock",
                                                                 "value": "locked"}])                       # own scene: visible
    r, req = last_chat(gw, "member")
    assert r.status_code == 200
    assert req["allowed_rooms"] == ["Bedroom", "Kitchen"]
    assert req["allowed_scenes"] == ["sc_00000001", "sc_00000003", "sc_00000009"]     # not sc_2 (Hall) nor sc_4 (room-less selector)


def test_guest_gets_its_room_scope_and_assigned_scenes_never_null(gw):
    r, req = last_chat(gw, "guest")
    assert r.status_code == 200 and req["allowed_rooms"] == ["Bedroom"] and req["allowed_scenes"] == []
    gw.ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gw.uid("guest"), "sc_00000001"))
    r, req = last_chat(gw, "guest")
    assert req["allowed_rooms"] == ["Bedroom"] and req["allowed_scenes"] == ["sc_00000001"]
    gw.ctx.auth.patch(gw.uid("guest"), rooms=[])
    r, req = last_chat(gw, "guest")
    assert r.status_code == 200 and req["allowed_rooms"] == []                       # no rooms = empty list, still not null


def test_scope_lookup_errors_fail_closed(gw):
    n = len(gw.m.agent.requests)
    orig = gw.ctx.hub.hub

    async def down(*a, **k):
        raise UpstreamError("device-hub")
    gw.ctx.hub.hub = down
    r = gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h("member"))
    assert r.status_code == 502 and gw.m.agent.requests[n:] == []                     # refused, nothing reached the agent
    gw.ctx.hub.hub = orig

    async def bad_status(*a, **k):
        return 500, None
    gw.ctx.hub.hub = bad_status
    assert gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h("member")).status_code == 502
    gw.ctx.hub.hub = orig
    gw.ctx.confirm.assigned = lambda user: (_ for _ in ()).throw(RuntimeError("db"))
    for who in ("member", "guest"):
        assert gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h(who)).status_code == 502
    assert gw.m.agent.requests[n:] == []
    assert gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h("admin")).status_code == 200      # admin needs no lookup


def test_a_refused_chat_does_not_burn_uploads(gw):
    ref = gw.pub.post("/images", files={"image": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")},
                      headers=gw.h("member")).json()["image_ref"]
    orig = gw.ctx.hub.hub

    async def down(*a, **k):
        raise UpstreamError("device-hub")
    gw.ctx.hub.hub = down
    assert gw.pub.post("/chat", json={"image_refs": [ref]}, headers=gw.h("member")).status_code == 502
    gw.ctx.hub.hub = orig
    assert gw.pub.post("/chat", json={"image_refs": [ref]}, headers=gw.h("member")).status_code == 200


# ------------------------------------------------------------------ 2. SseEvent / WsMessage conformance of real output
@pytest.mark.parametrize("who,text", [("admin", "hello"), ("member", "unlock the door"), ("admin", "open up"), ("member", "close the blinds")])
def test_every_proxied_sse_event_validates_against_SseEvent(gw, who, text):
    r = gw.pub.post("/chat", json={"text": text}, headers=gw.h(who))
    assert r.status_code == 200
    frames = sse_frames(r.text)
    assert frames and frames[-1]["event"] == "final"
    for f in frames:
        assert schema("SseEvent").is_valid(f), f
    assert "confirmation_token" not in r.text


def test_sse_validator_rejects_a_drifted_event():
    assert not valid("SseEvent", {"event": "final", "data": {"seq": 1, "req_id": "r", "text": "x", "route": "fast"}})   # no latency_ms
    assert not valid("SseEvent", {"event": "status", "data": {"text": "x"}})                                            # no seq / req_id


@pytest.fixture()
def sockets(gw):
    gw.ctx.agent_push = True
    with gw.pub:
        gw.pub.portal.call(lambda: gw.ctx.bc.room_of.update({"ep_00000001": "Bedroom", "ep_00000002": "Bedroom",
                                                             "ep_00000003": "Hall", "ep_00000004": "Kitchen", "ep_00000006": "Bedroom"}))
        opened = {r: connect(gw, r) for r in ("admin", "member", "guest")}
        yield {r: s for r, (_ws, s) in opened.items()}
        for ws, _ in opened.values():
            ws.__exit__(None, None, None)


def pub(gw, ev):
    gw.pub.portal.call(gw.ctx.bc.publish, ev)


def test_every_websocket_message_validates_against_WsMessage(gw, sockets):
    """Drives every message type the gateway can emit through the real paths and validates what a client receives."""
    me, admin = gw.uid("member"), gw.uid("admin")
    ts = int(time.time())
    # hub-origin events, as the hub sends them (extra fields, ms timestamps, no call_id on gateway-initiated commands)
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {"power": True}, "ts": ts}}))
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_00000006", "values": {"position": 80}, "ts": ts * 1000}}))
    pub(gw, from_hub({"type": "registry_changed", "data": {"version": 3, "reason": "patch", "endpoint_ids": ["ep_00000001"]}}))
    pub(gw, from_hub({"type": "action_settled", "data": {"endpoint_id": "ep_00000006", "capability": "position", "value": 80,
                                                         "observed": 80, "elapsed_ms": 4000}}))
    pub(gw, from_hub({"type": "action_settle_timeout", "data": {"endpoint_id": "ep_00000006", "capability": "position", "value": 80,
                                                                "observed": None, "settle_s": 30}}))
    pub(gw, from_hub({"type": "node_status", "data": {"node_id": NODE, "online": False}}))
    pub(gw, from_hub({"type": "pending_node", "data": {"node_id": NODE}}))
    # agent-origin events
    pub(gw, from_agent("async_result", {"user_id": me, "ref": "c1", "kind": "vision", "text": "a cat", "latency_ms": 5400, "extra": 1}))
    pub(gw, from_agent("action_settled", {"user_id": me, "call_id": "c2", "endpoint_id": "ep_00000001", "capability": "power",
                                          "value": True, "observed": True, "elapsed_ms": 3, "debug": "x"}))
    pub(gw, from_agent("action_settle_timeout", {"user_id": "household", "call_id": "c3", "endpoint_id": "ep_00000001",
                                                 "capability": "power", "value": True, "observed": None, "settle_s": 5}))
    pub(gw, from_agent("notification", {"user_id": "household", "level": "action_blocked", "text": "Rule wanted to unlock", "x": 1}))
    # confirmation lifecycle through the real code paths (chat stream -> request, decline, retry, expire)
    gw.pub.post("/chat", json={"text": "unlock the door"}, headers=gw.h("admin"))
    rid = gw.ctx.db.one("SELECT request_id FROM confirm_requests WHERE user_id=?", (admin,))["request_id"]
    gw.pub.post("/chat", json={"text": "open up"}, headers=gw.h("admin"))                  # a scene request too
    assert gw.pub.post(f"/confirmations/{rid}/decline", headers=gw.h("admin")).status_code == 204
    assert gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin")).status_code == 202
    gw.clock.advance(120)
    run(gw.ctx.confirm.sweep())                                                           # -> confirmation_expired
    # scenes_changed through the hub subscriber path
    pub(gw, Event("scenes_changed", {"scene_id": "sc_00000001", "change": "updated"}, admin_only=True))
    got = drain(gw, sockets)
    seen = set()
    for who, msgs in got.items():
        for m in msgs:
            assert schema("WsMessage").is_valid(m), (who, m)
            seen.add(m["type"])
    assert seen >= {"state", "registry_changed", "action_settled", "action_settle_timeout", "notification", "async_result",
                    "confirmation_request", "confirmation_declined", "confirmation_expired", "scenes_changed"}, seen
    adm = got["admin"]
    assert any(m["type"] == "state" and m["ts"] == ts for m in adm)                      # milliseconds became seconds
    assert all("reason" not in m for m in adm if m["type"] == "registry_changed")
    assert all(m["call_id"] for m in adm if m["type"].startswith("action_settle"))
    reqs = [m for m in adm if m["type"] == "confirmation_request"]
    assert any(m["action"]["type"] == "run_scene" and "scene_id" in m["action"] for m in reqs)    # the hashed (event) shape


def test_connected_message_and_scene_events_validate(gw, sockets):
    # the first message of every socket is consumed by connect(); re-check its shape explicitly
    ws = gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{gw.pub.post('/ws/ticket', headers=gw.h('member')).json()['ticket']}"])
    with ws as s:
        assert schema("WsMessage").is_valid(s.receive_json())


def test_ws_validator_rejects_drifted_messages():
    ok = {"type": "state", "endpoint_id": "ep_00000001", "values": {"power": True}, "ts": 1_700_000_000}
    assert valid("WsMessage", ok)
    assert not valid("WsMessage", {**ok, "extra": 1})
    assert not valid("WsMessage", {"type": "notification", "level": "info", "text": "connected", "role": "admin"})
    assert not valid("WsMessage", {"type": "notification", "level": "info", "text": "x" * 501})
    assert not valid("WsMessage", {"type": "registry_changed", "version": 1, "reason": "x", "endpoint_ids": []})


def test_malformed_upstream_events_are_dropped_not_forwarded():
    assert from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {}, "ts": 1_700_000_000}}) is None
    assert from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {"a": {"x": 1}}, "ts": 1_700_000_000}}) is None
    assert from_hub({"type": "state", "data": {"endpoint_id": "nope", "values": {"a": 1}, "ts": 1_700_000_000}}) is None
    assert from_hub({"type": "action_settled", "data": {"endpoint_id": "ep_00000001", "capability": "p", "value": 1,
                                                        "observed": None, "elapsed_ms": 1}}) is None       # settled needs an observed value
    assert from_agent("async_result", {"user_id": "u", "ref": "r", "kind": "audio", "text": "x", "latency_ms": 1}) is None
    assert from_agent("action_settled", {"user_id": "u", "endpoint_id": "ep_00000001", "capability": "p", "value": 1, "observed": 1,
                                         "elapsed_ms": 1}) is None                                        # agent events must carry call_id
    assert from_agent("final", {"user_id": "u"}) is None


# ------------------------------------------------------------------ 1. GET /admin/action-log
def seed_actions(gw) -> None:
    gw.clock.advance(10)
    run(refresh_rooms(gw.ctx.hub, gw.ctx.bc))
    h = lambda who: gw.h(who)                                                                       # noqa: E731
    gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": True}, headers=h("member"))          # done
    gw.clock.advance(10)
    gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked"}, headers=h("admin"))      # confirmation_required
    gw.clock.advance(10)
    gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": True}, headers=h("guest"))           # denied
    gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "locked"}, headers=h("member"))       # denied (room)
    gw.clock.advance(10)
    gw.pub.post("/devices/ep_00000006/set", json={"capability": "position", "value": 80}, headers=h("member"))         # moving
    gw.clock.advance(10)
    gw.pub.post("/scenes/sc_00000003/run", headers=h("admin"))                                                         # scene done
    gw.pub.post("/scenes/sc_00000002/run", headers=h("admin"))                                                         # scene blocked


def log(gw, who="admin", **params):
    r = gw.pub.get("/admin/action-log", params=params, headers=gw.h(who))
    return r


def test_action_log_is_admin_only(gw):
    assert gw.pub.get("/admin/action-log").status_code == 401
    assert log(gw, "member").status_code == 403 and log(gw, "guest").status_code == 403
    assert log(gw, "admin").status_code == 200


def test_action_log_rows_match_the_schema_exactly_and_hold_no_traces(gw):
    seed_actions(gw)
    r = log(gw)
    assert r.status_code == 200
    validate({"$ref": "#/components/schemas/ActionLogPage"}, r.json())
    assert schema("ActionLogPage").is_valid(r.json())
    items = r.json()["items"]
    assert r.json()["next_cursor"] is None and len(items) == 7
    assert [i["id"] for i in items] == sorted((i["id"] for i in items), reverse=True)          # newest first
    by = {(i["kind"], i["status"], i["endpoint_id"] or i["scene_id"]): i for i in items}
    done = by[("set_state", "done", "ep_00000001")]
    assert done["user_id"] == gw.uid("member") and done["source"] == "direct" and done["node_id"] == NODE
    assert done["capability"] == "power" and done["value"] is True and done["error"] is None and done["call_id"] is None
    assert by[("set_state", "confirmation_required", "ep_00000003")]["error"] == "confirmation_required"
    assert by[("set_state", "moving", "ep_00000006")]["value"] == 80
    assert by[("run_scene", "done", "sc_00000003")]["source"] == "scene"
    assert by[("run_scene", "confirmation_required", "sc_00000002")]["endpoint_id"] is None
    assert sorted(i["status"] for i in items if i["status"] == "denied") == ["denied", "denied"]
    # nothing but the schema's keys: no trace / prompt / token / chat columns
    assert all(set(i) == set(SPEC["components"]["schemas"]["ActionLogEntry"]["properties"]) for i in items)
    assert not (set(items[0]) - set(SPEC["components"]["schemas"]["ActionLogEntry"]["properties"]))


def test_action_log_does_not_contain_chat_text_tokens_or_traces(gw):
    gw.pub.post("/chat", json={"text": "SECRET-PROMPT-TEXT blinds"}, headers=gw.h("member"))
    tok = gw.pub.post("/confirmations", json={"action": LOCK}, headers=gw.h("admin")).json()["confirmation_token"]
    gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok}, headers=gw.h("admin"))
    body = log(gw).text
    assert "SECRET-PROMPT-TEXT" not in body and tok not in body and "trace" not in body and "prompt" not in body
    cols = {r["name"] for r in gw.ctx.db.all("PRAGMA table_info(action_log)")}
    assert not ({"text", "prompt", "trace", "args", "token", "tool_args"} & cols)


def test_action_log_filters(gw):
    seed_actions(gw)
    member, admin = gw.uid("member"), gw.uid("admin")
    ids = lambda **p: [(i["kind"], i["status"]) for i in log(gw, **p).json()["items"]]            # noqa: E731
    assert len(log(gw, user_id=member).json()["items"]) == 3 and len(log(gw, user_id=admin).json()["items"]) == 3
    assert log(gw, user_id="nobody").json() == {"items": [], "next_cursor": None}
    assert ids(status="moving") == [("set_state", "moving")]
    assert len(log(gw, status="denied").json()["items"]) == 2
    assert {i["endpoint_id"] for i in log(gw, endpoint_id="ep_00000001").json()["items"]} == {"ep_00000001"}
    assert len(log(gw, endpoint_id="ep_00000001").json()["items"]) == 2                          # member done + guest denied
    assert len(log(gw, node_id=NODE).json()["items"]) == 5                                        # the direct sets (scene rows have no node)
    assert log(gw, node_id="esp32-000000000000").json()["items"] == []
    rows = {(i["kind"], i["status"]): i["ts"] for i in log(gw).json()["items"]}
    t_move = rows[("set_state", "moving")]
    assert [i["status"] for i in log(gw, **{"from": t_move}).json()["items"] if i["kind"] == "set_state"] == ["moving"]     # inclusive
    assert all(i["ts"] < t_move for i in log(gw, to=t_move).json()["items"])                                                # exclusive
    assert len(log(gw, **{"from": t_move, "to": t_move}).json()["items"]) == 0
    both = log(gw, status="done", user_id=admin).json()["items"]                                 # AND
    assert [(i["kind"], i["status"]) for i in both] == [("run_scene", "done")]


def test_action_log_cursor_pagination_is_stable_and_complete(gw):
    seed_actions(gw)
    full = [i["id"] for i in log(gw).json()["items"]]
    got, cursor, pages = [], None, 0
    while True:
        p = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        j = log(gw, **p).json()
        assert schema("ActionLogPage").is_valid(j) and len(j["items"]) <= 3
        got += [i["id"] for i in j["items"]]
        pages += 1
        if pages == 1:                                                                            # an insert between pages shifts nothing
            gw.pub.post("/devices/ep_00000002/set", json={"capability": "power", "value": True}, headers=gw.h("member"))
        cursor = j["next_cursor"]
        if cursor is None:
            break
    assert got == full and pages == 3 and len(set(got)) == len(got)
    assert len(log(gw, limit=200).json()["items"]) == 8                                          # the page-1 insert is visible on a fresh read


def test_action_log_rejects_bad_input(gw):
    seed_actions(gw)
    for bad in ({"limit": 0}, {"limit": 201}, {"limit": "x"}, {"status": "weird"}, {"node_id": "nope"}, {"endpoint_id": "ep_1"},
                {"from": -1}, {"from": 20, "to": 10}, {"cursor": "not-a-cursor"}, {"cursor": "x" * 300}, {"user_id": "u" * 65}):
        r = log(gw, **bad)
        assert r.status_code == 400 and r.json()["error"] == "bad_request", bad
        assert schema("Error").is_valid(r.json())


def test_reading_the_action_log_is_audit_logged(gw):
    log(gw, status="done", user_id="someone")
    row = gw.ctx.db.one("SELECT * FROM audit_log WHERE action='admin.action_log.read'")
    assert row["actor"] == gw.uid("admin") and json.loads(row["detail"])["filters"] == ["status", "user_id"]


def test_moving_rows_settle_in_place_from_hub_and_agent_events(gw):
    gw.pub.post("/devices/ep_00000006/set", json={"capability": "position", "value": 80}, headers=gw.h("member"))
    row = log(gw).json()["items"][0]
    assert row["status"] == "moving"
    # a gateway-initiated settle (no call_id) closes the newest matching moving row
    note_settle(gw.ctx.actions, "action_settled", {"endpoint_id": "ep_00000006", "capability": "position", "value": 80,
                                                   "observed": 80, "elapsed_ms": 4100})
    j = log(gw).json()
    assert len(j["items"]) == 1 and j["items"][0]["id"] == row["id"] and j["items"][0]["status"] == "settled"
    assert j["items"][0]["elapsed_ms"] == 4100
    note_settle(gw.ctx.actions, "action_settle_timeout", {"endpoint_id": "ep_00000006", "capability": "position", "value": 80,
                                                          "observed": None, "settle_s": 30})
    assert log(gw).json()["items"][0]["status"] == "settled"                                       # idempotent: not re-opened


def test_agent_actions_are_logged_with_their_call_id_and_settle_by_it(gw):
    run(refresh_rooms(gw.ctx.hub, gw.ctx.bc))
    gw.pub.post("/chat", json={"text": "close the blinds"}, headers=gw.h("member"))
    items = log(gw).json()["items"]
    assert len(items) == 1
    a = items[0]
    assert (a["source"], a["kind"], a["status"], a["call_id"], a["endpoint_id"], a["node_id"]) == \
           ("agent", "set_state", "moving", "c1", "ep_00000006", NODE)
    assert a["user_id"] == gw.uid("member") and a["value"] == 80
    gw.m.agent.emit("action_settle_timeout", {"user_id": gw.uid("member"), "call_id": "c1", "endpoint_id": "ep_00000006",
                                              "capability": "position", "value": 80, "observed": 40, "settle_s": 30})
    gw.ctx.agent_push = True
    with gw.pub:
        gw.pub.portal.call(lambda: gw.ctx.start(retry_s=0.05))
        try:
            for _ in range(100):
                if log(gw).json()["items"][0]["status"] != "moving":
                    break
                time.sleep(0.05)
        finally:
            gw.pub.portal.call(gw.ctx.shutdown)
    assert log(gw).json()["items"][0]["status"] == "settle_timeout"


def test_declined_and_expired_confirmations_are_logged(gw):
    gw.pub.post("/chat", json={"text": "unlock the door"}, headers=gw.h("admin"))
    rid = gw.ctx.db.one("SELECT request_id FROM confirm_requests")["request_id"]
    assert gw.pub.post(f"/confirmations/{rid}/decline", headers=gw.h("admin")).status_code == 204
    new = gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin")).json()["request_id"]
    gw.clock.advance(120)
    run(gw.ctx.confirm.sweep())
    st = [i["status"] for i in log(gw).json()["items"]]
    assert st == ["expired", "declined"] and new
    for i in log(gw).json()["items"]:
        assert i["endpoint_id"] == "ep_00000003" and i["source"] == "agent"
