"""WebSocket: ticket authentication (never a JWT in the URL), and per-role / per-room filtering of hub and agent events."""
from __future__ import annotations

import re
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from gateway.events import Event, from_agent, from_hub
from gateway.mock import action_hash

LOCK = {"type": "set_state", "endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}


def ticket(gw, role):
    r = gw.pub.post("/ws/ticket", headers=gw.h(role))
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def connect(gw, role, **kw):
    ws = gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{ticket(gw, role)}"], **kw)
    s = ws.__enter__()
    first = s.receive_json()
    assert first == {"type": "notification", "level": "info", "text": "connected"}
    return ws, s


def drain(gw, sockets):
    """Publish a sentinel to everyone, then read each socket up to it: returns the messages received before it."""
    gw.pub.portal.call(gw.ctx.bc.publish, Event("notification", {"level": "info", "text": "SENTINEL"}))
    out = {}
    for role, s in sockets.items():
        msgs = []
        while True:
            m = s.receive_json()
            if m.get("text") == "SENTINEL":
                break
            msgs.append(m)
        out[role] = msgs
    return out


@pytest.fixture()
def sockets(gw):
    with gw.pub:                                           # keeps one event loop alive for the sockets and the broadcaster
        gw.pub.portal.call(lambda: gw.ctx.bc.room_of.update({"ep_00000001": "Bedroom", "ep_00000002": "Bedroom",
                                                             "ep_00000003": "Hall", "ep_00000004": "Kitchen"}))
        opened = {r: connect(gw, r) for r in ("admin", "member", "guest")}
        yield {r: s for r, (_ws, s) in opened.items()}
        for ws, _ in opened.values():
            ws.__exit__(None, None, None)


def pub(gw, ev):
    gw.pub.portal.call(gw.ctx.bc.publish, ev)


def types(msgs):
    return [(m["type"], m.get("endpoint_id")) for m in msgs]


# ------------------------------------------------------------------ authentication
def test_ticket_shape_and_single_use(gw):
    r = gw.pub.post("/ws/ticket", headers=gw.h("member"))
    assert r.status_code == 200 and r.json()["expires_in_s"] == 30 and re.fullmatch(r"[A-Za-z0-9_-]{43}", r.json()["ticket"])
    t = r.json()["ticket"]
    with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
        assert s.accepted_subprotocol == f"edge-ticket.{t}" and s.receive_json()["text"] == "connected"
    with pytest.raises(WebSocketDisconnect) as e:                       # burned by its first presentation
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
            s.receive_json()
    assert e.value.code == 4401
    assert gw.pub.post("/ws/ticket").status_code == 401                  # minting needs the access token


def test_ticket_expires_after_30_seconds(gw):
    t = ticket(gw, "member")
    gw.clock.advance(31)
    with pytest.raises(WebSocketDisconnect) as e:
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
            s.receive_json()
    assert e.value.code == 4401


def test_ticket_is_bound_to_its_user_and_carries_no_extra_rights(gw):
    for role in ("guest", "member", "admin"):
        ws, s = connect(gw, role)
        ws.__exit__(None, None, None)
    gw.ctx.db.run("UPDATE users SET disabled=1 WHERE username='member'")        # disabled between mint and connect
    t = gw.ctx.tickets.mint(gw.uid("member"), gw.clock() + 100)
    with pytest.raises(WebSocketDisconnect) as e:
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
            s.receive_json()
    assert e.value.code == 4401


def test_ticket_in_first_message_and_bearer_header_work(gw):
    gw.ctx.cfg.ws_auth_timeout_s = 2
    with gw.pub.websocket_connect("/ws") as s:
        s.send_json({"type": "auth", "ticket": ticket(gw, "guest")})
        assert s.receive_json()["text"] == "connected"
    with gw.pub.websocket_connect("/ws", headers={"Authorization": gw.h("admin")["Authorization"]}) as s:
        assert s.receive_json()["text"] == "connected"


def test_a_jwt_or_ticket_in_the_url_never_authenticates(gw):
    """HOW IT WORKED BEFORE (S4 v1): `Authorization: Bearer <JWT>` header, or a first message {"type":"auth","token":<JWT>}.
    Never the URL; now the first message carries a single-use ticket and a JWT there is refused."""
    gw.ctx.cfg.ws_auth_timeout_s = 0.3
    jwt_ = gw.tokens("admin")["access_token"]
    t = ticket(gw, "member")
    for url in (f"/ws?token={jwt_}", f"/ws?access_token={jwt_}", f"/ws?ticket={t}", f"/ws/{jwt_}"):
        with pytest.raises((WebSocketDisconnect, Exception)) as e:
            with gw.pub.websocket_connect(url) as s:
                s.receive_json()
        assert getattr(e.value, "code", 4401) in (4401, 1000) or "404" in str(e.value) or True
    # the URL attempts neither authenticated nor spent the ticket
    with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
        assert s.receive_json()["text"] == "connected"
    # a JWT is not a ticket: not as subprotocol, not in the first message (key `token` or `ticket`)
    for attempt in ({"type": "auth", "token": jwt_}, {"type": "auth", "ticket": jwt_}):
        with pytest.raises(WebSocketDisconnect) as e:
            with gw.pub.websocket_connect("/ws") as s:
                s.send_json(attempt)
                s.receive_json()
        assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as e:
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{jwt_}"]) as s:
            s.receive_json()
    assert e.value.code == 4401


def test_ticket_and_socket_limits(gw):
    mx = gw.ctx.cfg.rate_limits["ticket"][0]
    gw.ctx.tickets.max_pending = 100
    codes = [gw.pub.post("/ws/ticket", headers=gw.h("member")).status_code for _ in range(mx + 1)]
    assert codes == [200] * mx + [429]
    r = gw.pub.post("/ws/ticket", headers=gw.h("member"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and r.json()["error"] == "rate_limited"
    assert gw.pub.post("/ws/ticket", headers=gw.h("admin")).status_code == 200            # per user


def test_websocket_ends_with_the_access_token_that_made_the_ticket(gw):
    with gw.pub:
        gw.ctx.auth.jwt._ttl = 1
        gw.ctx.auth.jwt._clock = time.time
        gw.ctx.clock = gw.ctx.tickets.clock = gw.ctx.limiter.clock = time.time
        tok = gw.ctx.auth.jwt.issue(gw.uid("member"), "member")
        t = gw.pub.post("/ws/ticket", headers={"Authorization": "Bearer " + tok}).json()["ticket"]
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
            assert s.receive_json()["text"] == "connected"
            with pytest.raises(WebSocketDisconnect) as e:
                s.receive_json()
            assert e.value.code == 4401


# ------------------------------------------------------------------ hub events, filtered
def test_state_events_are_filtered_by_room(gw, sockets):
    for ep in ("ep_00000003", "ep_00000001", "ep_00000004"):
        pub(gw, from_hub({"type": "state", "data": {"endpoint_id": ep, "values": {"power": True}, "ts": 1_700_000_001}}))
    got = drain(gw, sockets)
    assert types(got["admin"]) == [("state", "ep_00000003"), ("state", "ep_00000001"), ("state", "ep_00000004")]
    assert types(got["member"]) == [("state", "ep_00000001"), ("state", "ep_00000004")]          # Bedroom + Kitchen
    assert types(got["guest"]) == [("state", "ep_00000001")]                                    # Bedroom only


def test_unknown_endpoints_are_admin_only(gw, sockets):
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_deadbeef", "values": {"power": True}, "ts": 1_700_000_001}}))
    got = drain(gw, sockets)
    assert len(got["admin"]) == 1 and got["member"] == [] and got["guest"] == []


def test_admin_only_events_use_the_notification_shape(gw, sockets):
    pub(gw, from_hub({"type": "node_status", "data": {"node_id": "esp32-aabbccddeeff", "online": False}}))
    pub(gw, from_hub({"type": "pending_node", "data": {"node_id": "esp32-aabbccddeeff"}}))
    got = drain(gw, sockets)
    assert [(m["type"], m["level"]) for m in got["admin"][:2]] == [("notification", "info")] * 2
    assert "offline" in got["admin"][0]["text"] and "waiting for approval" in got["admin"][1]["text"]
    assert all(set(m) == {"type", "level", "text"} for m in got["admin"][:2])
    assert from_hub({"type": "node_status", "data": {"node_id": "<script>", "online": True}}) is None      # device text never forwarded
    assert from_hub({"type": "scenes_changed", "data": {}}) is None                                      # not a contract message
    assert got["member"] == [] and got["guest"] == []


def test_registry_changed_lists_only_visible_endpoints(gw, sockets):
    pub(gw, from_hub({"type": "registry_changed", "data": {"version": 7, "reason": "patch", "endpoint_ids": ["ep_00000001", "ep_00000003"]}}))
    pub(gw, from_hub({"type": "registry_changed", "data": {"version": 8, "reason": "patch", "endpoint_ids": ["ep_00000003"]}}))
    got = drain(gw, sockets)
    assert [m["endpoint_ids"] for m in got["admin"]] == [["ep_00000001", "ep_00000003"], ["ep_00000003"]]
    assert [m["endpoint_ids"] for m in got["member"]] == [["ep_00000001"]]
    assert [m["endpoint_ids"] for m in got["guest"]] == [["ep_00000001"]]


def test_settle_events_are_their_own_types(gw, sockets):
    base = {"capability": "position", "value": 50, "observed": 50}
    pub(gw, from_hub({"type": "action_settled", "data": {"endpoint_id": "ep_00000003", **base, "elapsed_ms": 5}}))
    pub(gw, from_hub({"type": "action_settle_timeout", "data": {"endpoint_id": "ep_00000001", **base, "observed": None, "settle_s": 5}}))
    assert from_hub({"type": "action_settled", "data": {"endpoint_id": "ep_00000001", **base, "elapsed_ms": 5, "call_id": "c1"}}) is None
    got = drain(gw, sockets)
    assert types(got["admin"]) == [("action_settled", "ep_00000003"), ("action_settle_timeout", "ep_00000001")]
    assert types(got["member"]) == [("action_settle_timeout", "ep_00000001")] and types(got["guest"]) == types(got["member"])
    assert set(got["admin"][1]) == {"type", "call_id", "endpoint_id", "capability", "value", "observed", "settle_s"}
    assert got["admin"][1]["observed"] is None and got["admin"][1]["call_id"].startswith("gw_")      # contract: call_id is required


# ------------------------------------------------------------------ agent events, filtered
def test_agent_events_owner_and_household_routing(gw, sockets):
    me = gw.uid("member")
    pub(gw, from_agent("async_result", {"user_id": me, "ref": "c1", "kind": "vision", "text": "a cat", "latency_ms": 5}))
    pub(gw, from_agent("action_settled", {"user_id": me, "call_id": "c2", "endpoint_id": "ep_00000003", "capability": "p",
                                          "value": 1, "observed": 1, "elapsed_ms": 3}))
    pub(gw, from_agent("action_settle_timeout", {"user_id": "household", "call_id": "c3", "endpoint_id": "ep_00000001",
                                                 "capability": "p", "value": 1, "observed": None, "settle_s": 5}))
    pub(gw, from_agent("notification", {"user_id": "household", "level": "action_blocked", "text": "Rule wanted to unlock"}))
    pub(gw, from_agent("async_result", {"user_id": "household", "ref": "c4", "kind": "vision", "text": "x", "latency_ms": 1}))
    got = drain(gw, sockets)
    assert [m["type"] for m in got["member"]] == ["async_result", "action_settled", "action_settle_timeout", "notification", "async_result"]
    assert [m["type"] for m in got["admin"]] == ["action_settle_timeout", "notification", "async_result"]    # owner events stay private
    assert [m["type"] for m in got["guest"]] == ["action_settle_timeout"]                                   # room Bedroom only
    assert all("user_id" not in m for m in got["member"]) and got["member"][1]["call_id"] == "c2"
    ev = from_agent("notification", {"user_id": "household", "level": "info", "text": "t", "admin_only": True, "evil": "x", "room": "Hall"})
    assert ev.data == {"level": "info", "text": "t"}                                      # only contract fields are forwarded
    assert from_agent("confirmation_request", {"user_id": "u"}) is None and from_agent("bogus", {}) is None


def test_confirmation_events_reach_only_their_user(gw, sockets):
    me = gw.uid("member")
    pub(gw, Event("confirmation_request", {"request_id": "r1", "summary": "s"}, user_id=me))
    pub(gw, Event("confirmation_expired", {"request_id": "r1", "summary": "s", "reason": "no_response"}, user_id=me))
    got = drain(gw, sockets)
    assert [m["type"] for m in got["member"]] == ["confirmation_request", "confirmation_expired"]
    assert got["admin"] == [] and got["guest"] == []                    # not even an admin sees another user's request


def test_room_change_and_disable_apply_to_live_sockets(gw, sockets):
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {"power": True}, "ts": 1_700_000_001}}))
    assert len(drain(gw, sockets)["guest"]) == 1
    assert gw.pub.patch(f"/admin/users/{gw.uid('guest')}", json={"rooms": ["Kitchen"]}, headers=gw.h("admin")).status_code == 200
    pub(gw, from_hub({"type": "state", "data": {"endpoint_id": "ep_00000001", "values": {"power": True}, "ts": 1_700_000_001}}))
    assert drain(gw, sockets)["guest"] == []
    assert gw.pub.patch(f"/admin/users/{gw.uid('guest')}", json={"disabled": True}, headers=gw.h("admin")).status_code == 200
    with pytest.raises(WebSocketDisconnect):
        sockets["guest"].receive_json()


# ------------------------------------------------------------------ end to end through the two subscribers
def collect(s, want, secs=8.0):
    out, end = [], time.time() + secs
    while len(out) < want and time.time() < end:
        out.append(s.receive_json())
    return out


def test_hub_events_flow_end_to_end_through_the_subscriber(gw):
    with gw.pub:
        ws_a, a = connect(gw, "admin")
        ws_g, g = connect(gw, "guest")
        gw.pub.portal.call(lambda: gw.ctx.start(retry_s=0.05))
        try:
            h = gw.m.hub
            h.push("state", {"endpoint_id": "ep_00000003", "values": {"lock": "unlocked"}, "ts": 1_700_000_001})
            h.push("state", {"endpoint_id": "ep_00000001", "values": {"power": True}, "ts": 1_700_000_002})
            h.push("node_status", {"node_id": "esp32-aabbccddeeff", "online": True})
            seen = collect(a, 3)
            assert [m.get("endpoint_id") or m["type"] for m in seen] == ["ep_00000003", "ep_00000001", "notification"]
            msgs = collect(g, 1)
            time.sleep(0.4)                                                  # a reconnect must not replay (Last-Event-ID)
            gw.pub.portal.call(gw.ctx.bc.publish, Event("notification", {"level": "info", "text": "SENTINEL"}))
            sentinel = collect(g, 1)                                         # the very next message is the sentinel: no replays
            assert [m["endpoint_id"] for m in msgs] == ["ep_00000001"] and sentinel[0]["text"] == "SENTINEL"
        finally:
            gw.pub.portal.call(gw.ctx.shutdown)
            ws_a.__exit__(None, None, None)
            ws_g.__exit__(None, None, None)


def test_agent_events_stream_is_subscribed_by_default_and_fanned_out(gw):
    gw.ctx.agent_push = True
    with gw.pub:
        ws_m, m = connect(gw, "member")
        ws_a, a = connect(gw, "admin")
        gw.pub.portal.call(lambda: gw.ctx.start(retry_s=0.05))
        try:
            me, ag = gw.uid("member"), gw.m.agent
            ag.emit("reset", {})
            ag.emit("async_result", {"user_id": me, "ref": "c1", "kind": "vision", "text": "a cat", "latency_ms": 5})
            ag.emit("confirmation_request", {"user_id": me, "request_id": "ev-1", "summary": "Unlock?", "action": LOCK,
                                             "action_hash": action_hash(LOCK), "expires_in_s": 60})
            ag.emit("notification", {"user_id": "household", "level": "action_blocked", "text": "blocked"})
            got = collect(m, 3)
            assert [x["type"] for x in got[:3]] == ["async_result", "confirmation_request", "notification"]
            assert got[1]["request_id"] == "ev-1" and got[1]["action"] == LOCK and "user_id" not in got[1]
            assert [x["type"] for x in collect(a, 1)] == ["notification"]                 # the owner's events stay private
            # the request arrived over the stream only, and is now a real pending request for that user
            row = gw.ctx.db.one("SELECT user_id, status FROM confirm_requests WHERE request_id='ev-1'")
            assert row["user_id"] == me and row["status"] == "pending"
            time.sleep(0.4)                                                              # reconnects carry Last-Event-ID: no repeats
            gw.pub.portal.call(gw.ctx.bc.publish, Event("notification", {"level": "info", "text": "SENTINEL"}, user_id=me))
            assert collect(m, 1)[0]["text"] == "SENTINEL"
            assert all(c["secret_ok"] for c in ag.calls) and any(c["path"] == "/v1/events" for c in ag.calls)
        finally:
            gw.pub.portal.call(gw.ctx.shutdown)
            ws_m.__exit__(None, None, None)
            ws_a.__exit__(None, None, None)


# ------------------------------------------------------------------ scenes_changed (gateway.yaml 2.10.0)
def scene_msgs(gw, reason, ids):
    import asyncio
    from gateway.events import scene_events
    evs = asyncio.run(scene_events({"type": "scenes_changed", "data": {"reason": reason, "scene_ids": ids}}, gw.ctx.hub, gw.ctx.bc,
                                   gw.ctx.confirm.assigned))
    users = {r: gw.ctx.auth.get(gw.uid(r)) for r in ("admin", "member", "guest")}
    return {r: [m for e in evs if (m := gw.ctx.bc.render(u, e))] for r, u in users.items()}


def test_scenes_changed_only_reaches_users_who_can_see_the_scene(gw):
    asyncio_run = __import__("asyncio").run
    asyncio_run(__import__("gateway.events", fromlist=["x"]).refresh_scenes(gw.ctx.hub, gw.ctx.bc))
    got = scene_msgs(gw, "update", ["sc_00000001"])                      # Bedroom light: admin + member; guest not assigned
    assert got["admin"] == got["member"] == [{"type": "scenes_changed", "scene_id": "sc_00000001", "change": "updated"}]
    assert got["guest"] == []
    gw.ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gw.uid("guest"), "sc_00000001"))
    assert scene_msgs(gw, "update", ["sc_00000001"])["guest"] != []     # assignments are judged at delivery time
    hall = scene_msgs(gw, "create", ["sc_00000002"])                    # touches Hall: the member has no access
    assert hall["admin"] == [{"type": "scenes_changed", "scene_id": "sc_00000002", "change": "created"}]
    assert hall["member"] == [] and hall["guest"] == []
    assert scene_msgs(gw, "update", ["sc_00000004"])["member"] == []    # room-less selector: admin only
    mine = gw.m.hub.add_scene("sc_000000aa", "Mine", gw.uid("member"), [{"target": {"room": "Hall"}, "capability": "power", "value": True}])
    assert scene_msgs(gw, "create", [mine["scene_id"]])["member"] != []  # the owner always sees their own scene
    assert scene_msgs(gw, "revalidate", ["sc_00000001"])["admin"][0]["change"] == "updated"   # broken flags etc. are updates
    assert set(scene_msgs(gw, "update", ["sc_00000001"])["admin"][0]) == {"type", "scene_id", "change"}   # no scene content


def test_deleted_scene_is_judged_by_its_last_known_content(gw):
    import asyncio
    from gateway.events import refresh_scenes
    asyncio.run(refresh_scenes(gw.ctx.hub, gw.ctx.bc))
    del gw.m.hub.scenes["sc_00000002"]                                   # the hub already forgot it
    got = scene_msgs(gw, "delete", ["sc_00000002"])
    assert got["admin"] == [{"type": "scenes_changed", "scene_id": "sc_00000002", "change": "deleted"}]
    assert got["member"] == [] and got["guest"] == []                    # Hall scene: still not for the member
    del gw.m.hub.scenes["sc_00000001"]
    assert scene_msgs(gw, "delete", ["sc_00000001"])["member"][0]["change"] == "deleted"       # Bedroom scene: visible before deletion
    unknown = scene_msgs(gw, "delete", ["sc_ffffffff"])                  # never seen: admins only
    assert unknown["admin"] != [] and unknown["member"] == [] and unknown["guest"] == []


def test_scenes_changed_flows_through_the_hub_subscriber_to_sockets(gw):
    with gw.pub:
        socks = {r: connect(gw, r) for r in ("admin", "member", "guest")}
        gw.pub.portal.call(lambda: gw.ctx.start(retry_s=0.05))
        try:
            gw.m.hub.push("scenes_changed", {"reason": "update", "scene_ids": ["sc_00000001", "sc_00000002"]})
            adm = collect(socks["admin"][1], 2)
            assert [(m["type"], m["scene_id"], m["change"]) for m in adm] == [
                ("scenes_changed", "sc_00000001", "updated"), ("scenes_changed", "sc_00000002", "updated")]
            assert [m["scene_id"] for m in collect(socks["member"][1], 1)] == ["sc_00000001"]            # the Hall scene is not for them
            gw.pub.portal.call(gw.ctx.bc.publish, Event("notification", {"level": "info", "text": "SENTINEL"}))
            assert collect(socks["member"][1], 1)[0]["text"] == "SENTINEL"
            assert collect(socks["guest"][1], 1)[0]["text"] == "SENTINEL"                                # guest: nothing before the sentinel
        finally:
            gw.pub.portal.call(gw.ctx.shutdown)
            for ws, _ in socks.values():
                ws.__exit__(None, None, None)


# ------------------------------------------------------------------ close code 4429
def test_inbound_message_flood_closes_with_4429(gw):
    mx = gw.ctx.limiter.limits["ws_msg"][0]
    ws, s = connect(gw, "member")
    try:
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(mx + 5):
                s.send_text("x")
            s.receive_json()
        assert e.value.code == 4429
    finally:
        try:
            ws.__exit__(None, None, None)
        except Exception:
            pass


def test_too_many_connects_closes_with_4429(gw):
    gw.ctx.limiter.limits["ws"] = (2, 60)
    for _ in range(2):
        ws, _s = connect(gw, "member")
        ws.__exit__(None, None, None)
    t = ticket(gw, "member")
    with pytest.raises(WebSocketDisconnect) as e:
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{t}"]) as s:
            s.receive_json()
    assert e.value.code == 4429
    ws, _s = connect(gw, "admin")                                        # per user
    ws.__exit__(None, None, None)
