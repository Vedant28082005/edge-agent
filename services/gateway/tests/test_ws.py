"""WebSocket fan-out: authentication and per-role / per-room filtering (hub events and agent events)."""
from __future__ import annotations

import time

import pytest
from starlette.websockets import WebSocketDisconnect

from gateway.events import Event, from_agent, from_hub


def open_ws(gw, role):
    ws = gw.pub.websocket_connect("/ws", headers={"Authorization": gw.h(role)["Authorization"]})
    s = ws.__enter__()
    assert s.receive_json() == {"type": "notification", "kind": "ws_ready", "role": role}
    return ws, s


def drain(gw, sockets, sentinel_user=None):
    """Publish a sentinel to everyone, then read each socket up to it: returns the messages received before it."""
    gw.pub.portal.call(gw.ctx.bc.publish, Event("notification", {"kind": "sentinel"}))
    out = {}
    for role, s in sockets.items():
        msgs = []
        while True:
            m = s.receive_json()
            if m.get("kind") == "sentinel":
                break
            msgs.append(m)
        out[role] = msgs
    return out


@pytest.fixture()
def sockets(gw):
    with gw.pub:                                           # keeps one event loop alive for the sockets and the broadcaster
        gw.pub.portal.call(lambda: gw.ctx.bc.room_of.update({"ep_00000001": "Bedroom", "ep_00000002": "Bedroom",
                                                             "ep_00000003": "Hall", "ep_00000004": "Kitchen"}))
        opened = {r: open_ws(gw, r) for r in ("admin", "member", "guest")}
        yield {r: s for r, (_ws, s) in opened.items()}
        for ws, _ in opened.values():
            ws.__exit__(None, None, None)


def pub(gw, ev):
    gw.pub.portal.call(gw.ctx.bc.publish, ev)


def kinds(msgs):
    return [(m["type"], m.get("kind") or m.get("endpoint_id")) for m in msgs]


def test_ws_needs_a_valid_access_token(gw):
    for first in ({"type": "auth", "token": "x"}, {"hello": 1}):
        with pytest.raises(WebSocketDisconnect) as e:
            with gw.pub.websocket_connect("/ws") as s:
                s.send_json(first)
                s.receive_json()
        assert e.value.code == 4401


def test_first_message_auth_works(gw):
    with gw.pub.websocket_connect("/ws") as s:
        s.send_json({"type": "auth", "token": gw.tokens("guest")["access_token"]})
        assert s.receive_json()["role"] == "guest"


def test_state_events_are_filtered_by_room(gw, sockets):
    for ep in ("ep_00000003", "ep_00000001", "ep_00000004"):
        pub(gw, from_hub({"type": "state", "data": {"endpoint_id": ep, "values": {"power": True}, "ts": 1}}))
    got = drain(gw, sockets)
    assert kinds(got["admin"]) == [("state", "ep_00000003"), ("state", "ep_00000001"), ("state", "ep_00000004")]
    assert kinds(got["member"]) == [("state", "ep_00000001"), ("state", "ep_00000004")]          # Bedroom + Kitchen
    assert kinds(got["guest"]) == [("state", "ep_00000001")]                                    # Bedroom only


def test_unknown_endpoints_are_admin_only(gw, sockets):
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_deadbeef", "values": {}, "ts": 1}}))
    got = drain(gw, sockets)
    assert len(got["admin"]) == 1 and got["member"] == [] and got["guest"] == []


def test_admin_only_events(gw, sockets):
    pub(gw, from_hub({"type": "node_status", "data": {"node_id": "esp32-aabbccddeeff", "online": False}}))
    pub(gw, from_hub({"type": "pending_node", "data": {"node_id": "esp32-aabbccddeeff"}}))
    pub(gw, from_agent("notification", {"text": "intrusion", "admin_only": True}))
    got = drain(gw, sockets)
    assert [m.get("kind") or m["type"] for m in got["admin"]] == ["node_status", "pending_node", "notification"]
    assert got["member"] == [] and got["guest"] == []


def test_registry_changed_lists_only_visible_endpoints(gw, sockets):
    pub(gw, from_hub({"type": "registry_changed", "data": {"version": 7, "reason": "patch", "endpoint_ids": ["ep_00000001", "ep_00000003"]}}))
    pub(gw, from_hub({"type": "registry_changed", "data": {"version": 8, "reason": "patch", "endpoint_ids": ["ep_00000003"]}}))
    got = drain(gw, sockets)
    assert [m["endpoint_ids"] for m in got["admin"]] == [["ep_00000001", "ep_00000003"], ["ep_00000003"]]
    assert [m["endpoint_ids"] for m in got["member"]] == [["ep_00000001"]]
    assert [m["endpoint_ids"] for m in got["guest"]] == [["ep_00000001"]]


def test_settle_events_and_scenes_changed(gw, sockets):
    pub(gw, from_hub({"type": "action_settled", "data": {"endpoint_id": "ep_00000003", "capability": "p", "value": 1, "observed": 1,
                                                          "elapsed_ms": 5, "call_id": "c1"}}))
    pub(gw, from_hub({"type": "action_settle_timeout", "data": {"endpoint_id": "ep_00000001", "capability": "p", "value": 1,
                                                                 "observed": None, "settle_s": 5}}))
    pub(gw, from_hub({"type": "scenes_changed", "data": {}}))
    got = drain(gw, sockets)
    assert [m.get("kind") for m in got["admin"]] == ["action_settled", "action_settle_timeout", "scenes_changed"]
    assert [m.get("kind") for m in got["member"]] == ["action_settle_timeout", "scenes_changed"]
    assert [m.get("kind") for m in got["guest"]] == ["action_settle_timeout"]                   # guests: no scene changes
    assert "call_id" not in got["admin"][0]


def test_agent_events_are_routed_by_hints(gw, sockets):
    me = gw.uid("member")
    pub(gw, from_agent("async_result", {"ref": "c1", "kind": "vision", "text": "a cat", "user_id": me}))
    pub(gw, from_agent("notification", {"level": "action_blocked", "text": "Rule wanted to unlock"}))        # no hint: not for guests
    pub(gw, from_agent("perception", {"label": "person", "room": "Hall"}))
    pub(gw, from_agent("perception", {"label": "person", "room": "Bedroom"}))
    got = drain(gw, sockets)
    assert [m["type"] for m in got["member"]] == ["async_result", "notification", "perception"]      # own result, notice, Bedroom
    assert [m["type"] for m in got["admin"]] == ["notification", "perception", "perception"]          # others' async results stay private
    assert [m["type"] for m in got["guest"]] == ["perception"]                                      # Bedroom only
    assert from_agent("bogus", {}) is None


def test_confirmation_events_reach_only_their_user(gw, sockets):
    me = gw.uid("member")
    pub(gw, Event("confirmation_request", {"request_id": "r1", "summary": "s"}, user_id=me))
    got = drain(gw, sockets)
    assert [m["type"] for m in got["member"]] == ["confirmation_request"]
    assert got["admin"] == [] and got["guest"] == []                    # not even an admin sees another user's request


def test_room_change_and_disable_apply_to_live_sockets(gw, sockets):
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {}, "ts": 1}}))
    assert len(drain(gw, sockets)["guest"]) == 1
    assert gw.pub.patch(f"/admin/users/{gw.uid('guest')}", json={"rooms": ["Kitchen"]}, headers=gw.h("admin")).status_code == 200
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {}, "ts": 1}}))
    assert drain(gw, sockets)["guest"] == []
    assert gw.pub.patch(f"/admin/users/{gw.uid('guest')}", json={"disabled": True}, headers=gw.h("admin")).status_code == 200
    with pytest.raises(WebSocketDisconnect):
        sockets["guest"].receive_json()


def test_hub_events_flow_end_to_end_through_the_subscriber(gw):
    with gw.pub:
        ws_a, a = open_ws(gw, "admin")
        ws_g, g = open_ws(gw, "guest")
        gw.pub.portal.call(lambda: gw.ctx.start(retry_s=0.05))
        try:
            h = gw.m.hub
            h.push("state", {"endpoint_id": "ep_00000003", "values": {"lock": "unlocked"}, "ts": 1})
            h.push("state", {"endpoint_id": "ep_00000001", "values": {"power": True}, "ts": 2})
            h.push("node_status", {"node_id": "esp32-aabbccddeeff", "online": True})
            t0, seen = time.time(), []
            while len(seen) < 3 and time.time() - t0 < 10:
                seen.append(a.receive_json())
            assert [m.get("endpoint_id") or m.get("kind") for m in seen] == ["ep_00000003", "ep_00000001", "node_status"]
            gw.pub.portal.call(gw.ctx.bc.publish, Event("notification", {"kind": "sentinel"}))
            msgs = []
            while True:
                m = g.receive_json()
                if m.get("kind") == "sentinel":
                    break
                msgs.append(m)
            assert [m["endpoint_id"] for m in msgs] == ["ep_00000001"]                       # no Hall, no node status for a guest
        finally:
            gw.pub.portal.call(gw.ctx.shutdown)
            ws_a.__exit__(None, None, None)
            ws_g.__exit__(None, None, None)


def test_websocket_ends_with_its_access_token(gw):
    with gw.pub:
        gw.ctx.cfg.access_ttl_s = 1
        gw.ctx.auth.jwt._ttl = 1
        gw.ctx.auth.jwt._clock = time.time          # real clock for this one
        gw.ctx.clock = time.time
        tok = gw.ctx.auth.jwt.issue(gw.uid("member"), "member")
        with gw.pub.websocket_connect("/ws", headers={"Authorization": "Bearer " + tok}) as s:
            assert s.receive_json()["kind"] == "ws_ready"
            with pytest.raises(WebSocketDisconnect):
                s.receive_json()
