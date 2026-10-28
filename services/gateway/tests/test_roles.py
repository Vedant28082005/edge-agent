"""admin/member/guest matrix on EVERY public route (plus anonymous), and the finer role rules."""
from __future__ import annotations

import pytest

from calls import SCENE_BODY, build, call, own_scene
from test_contract import PUBLIC

ALL = {"admin", "member", "guest"}
ADMIN = {"admin"}
NO_GUEST = {"admin", "member"}
OPEN = {"login", "refresh"}                 # no auth at all
ALLOWED = {
    "listDevices": ALL, "setDeviceState": NO_GUEST, "listScenes": ALL, "createScene": NO_GUEST, "updateScene": NO_GUEST,
    "deleteScene": NO_GUEST, "runScene": ALL, "mintConfirmation": NO_GUEST, "declineConfirmation": ALL,
    "retryConfirmation": NO_GUEST,
    **{k: NO_GUEST for k in ("listAutomations", "createAutomation", "getAutomation", "updateAutomation", "deleteAutomation",
                              "approveAutomation", "disableAutomation", "listAutomationRuns", "listNotifications", "markNotificationsRead")}, "createWsTicket": ALL, "chat": ALL, "uploadVoice": ALL, "uploadImage": ALL, "listMyMemories": ALL,
    "updateMyMemory": NO_GUEST, "deleteMyMemory": NO_GUEST, "systemHealth": ALL,
    **{op["operationId"]: ADMIN for _, _, op in PUBLIC if op.get("x-required-role") == "admin"},
}
OPIDS = [op["operationId"] for _, _, op in PUBLIC if op["operationId"] not in OPEN | {"websocket"}]


def test_matrix_covers_every_operation():
    assert set(OPIDS) == set(ALLOWED), set(OPIDS) ^ set(ALLOWED)


@pytest.mark.parametrize("op", OPIDS)
def test_role_matrix(gw, op):
    # anonymous: always 401
    spec = build(gw, op, "admin")
    assert gw.pub.request(spec["method"], spec["url"], **spec["kw"]).status_code == 401
    for role in ("guest", "member", "admin"):
        r, want = call(gw, op, role)
        if role in ALLOWED[op]:
            assert r.status_code == want, f"{op} as {role}: {r.status_code} {r.text[:150]}"
        else:
            assert r.status_code == 403, f"{op} as {role} should be 403, got {r.status_code} {r.text[:150]}"
            assert r.json()["error"] == "forbidden"


def test_admin_routes_never_reach_the_hub_for_non_admins(gw):
    before = len(gw.m.hub.calls)
    for op in [o for o, a in ALLOWED.items() if a == ADMIN]:
        for role in ("guest", "member"):
            assert call(gw, op, role)[0].status_code == 403
    assert len(gw.m.hub.calls) == before


# ---------------------------------------------------------------- finer rules
def test_member_cannot_touch_rooms_they_lack(gw):
    r = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked"}, headers=gw.h("member"))   # Hall
    assert r.status_code == 403
    assert gw.pub.post("/confirmations", json={"action": {"type": "set_state", "endpoint_id": "ep_00000003", "capability": "lock",
                                                          "value": "unlocked"}}, headers=gw.h("member")).status_code == 403
    assert gw.pub.get("/devices?room=Hall", headers=gw.h("member")).json() == []


def test_members_create_scenes_only_for_their_rooms(gw):
    ok = gw.pub.post("/scenes", json=SCENE_BODY, headers=gw.h("member"))                       # Bedroom light
    assert ok.status_code == 201 and ok.json()["owner"] == gw.uid("member")
    hall = {"name": "h", "actions": [{"target": {"endpoint_id": "ep_00000003"}, "capability": "lock", "value": "locked"}]}
    assert gw.pub.post("/scenes", json=hall, headers=gw.h("member")).status_code == 403
    sel_hall = {"name": "h", "actions": [{"target": {"room": "Hall"}, "capability": "power", "value": False}]}
    assert gw.pub.post("/scenes", json=sel_hall, headers=gw.h("member")).status_code == 403
    sel_any = {"name": "h", "actions": [{"target": {"type": "light"}, "capability": "power", "value": False}]}   # room-less selector
    assert gw.pub.post("/scenes", json=sel_any, headers=gw.h("member")).status_code == 403
    mixed = {"name": "h", "actions": [SCENE_BODY["actions"][0], hall["actions"][0]]}
    assert gw.pub.post("/scenes", json=mixed, headers=gw.h("member")).status_code == 403
    assert gw.pub.post("/scenes", json=hall, headers=gw.h("admin")).status_code == 201          # admin: anything
    assert gw.pub.post("/scenes", json={"name": "x", "actions": [{"target": {"bogus": 1}, "capability": "p", "value": 1}]},
                       headers=gw.h("admin")).status_code == 400


def test_member_edits_and_deletes_own_scenes_only(gw):
    other = own_scene(gw, gw.uid("admin"))
    mine = own_scene(gw, gw.uid("member"))
    assert gw.pub.put(f"/scenes/{other}", json=SCENE_BODY, headers=gw.h("member")).status_code == 403
    assert gw.pub.delete(f"/scenes/{other}", headers=gw.h("member")).status_code == 403
    assert gw.pub.put(f"/scenes/{mine}", json=SCENE_BODY, headers=gw.h("member")).status_code == 200
    hall = {"name": "h", "actions": [{"target": {"endpoint_id": "ep_00000003"}, "capability": "lock", "value": "locked"}]}
    assert gw.pub.put(f"/scenes/{mine}", json=hall, headers=gw.h("member")).status_code == 403    # cannot move it into a foreign room
    assert gw.pub.put(f"/scenes/{other}", json=SCENE_BODY, headers=gw.h("admin")).status_code == 200
    assert gw.pub.delete(f"/scenes/{other}", headers=gw.h("admin")).status_code == 204
    assert gw.pub.put("/scenes/sc_ffffffff", json=SCENE_BODY, headers=gw.h("admin")).status_code == 404


def test_scene_assignment_admin_routes(gw):
    a, guest = gw.h("admin"), gw.uid("guest")
    assert gw.pub.get("/admin/scenes/sc_00000001/assignments", headers=a).json() == {"user_ids": []}
    assert gw.pub.get("/admin/scenes/sc_ffffffff/assignments", headers=a).status_code == 404
    assert gw.pub.put("/admin/scenes/sc_ffffffff/assignments", json={"user_ids": []}, headers=a).status_code == 404
    for bad in ([gw.uid("member")], [gw.uid("admin")], ["u_nope"], [guest, guest]):
        assert gw.pub.put("/admin/scenes/sc_00000001/assignments", json={"user_ids": bad}, headers=a).status_code == 400, bad
    assert gw.pub.put("/admin/scenes/sc_00000001/assignments", json={"user_ids": [guest]}, headers=a).json() == {"user_ids": [guest]}
    assert gw.pub.post("/scenes/sc_00000001/run", headers=gw.h("guest")).status_code == 200
    assert gw.pub.put("/admin/scenes/sc_00000001/assignments", json={"user_ids": []}, headers=a).json() == {"user_ids": []}
    assert gw.pub.post("/scenes/sc_00000001/run", headers=gw.h("guest")).status_code == 403          # whole set replaced
    gw.pub.put("/admin/scenes/sc_00000001/assignments", json={"user_ids": [guest]}, headers=a)
    gw.pub.delete(f"/admin/users/{guest}", headers=a)                                              # a deleted user drops out
    assert gw.ctx.db.one("SELECT COUNT(*) c FROM scene_assignments")["c"] == 0
    gw.pub.put("/admin/scenes/sc_00000003/assignments", json={"user_ids": []}, headers=a)
    s = gw.pub.post("/scenes", json=SCENE_BODY, headers=a).json()["scene_id"]
    gw.ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gw.uid("admin"), s))
    gw.pub.delete(f"/scenes/{s}", headers=a)
    assert gw.ctx.db.one("SELECT COUNT(*) c FROM scene_assignments WHERE scene_id=?", (s,))["c"] == 0


def test_guest_runs_assigned_scenes_only_and_sees_only_those(gw):
    guest = gw.h("guest")
    assert gw.pub.get("/scenes", headers=guest).json() == []
    assert gw.pub.post("/scenes/sc_00000001/run", headers=guest).status_code == 403
    gw.ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gw.uid("guest"), "sc_00000001"))
    assert [s["scene_id"] for s in gw.pub.get("/scenes", headers=guest).json()] == ["sc_00000001"]
    assert gw.pub.post("/scenes/sc_00000001/run", headers=guest).status_code == 200
    assert gw.pub.post("/scenes/sc_00000003/run", headers=guest).status_code == 403            # not assigned
    for fn in (lambda: gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": True}, headers=guest),
               lambda: gw.pub.post("/scenes", json=SCENE_BODY, headers=guest)):
        assert fn().status_code == 403


def test_guest_never_confirms_risky_actions(gw):
    guest = gw.h("guest")
    gw.ctx.db.run("INSERT INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (gw.uid("guest"), "sc_00000002"))
    r = gw.pub.post("/scenes/sc_00000002/run", headers=guest)                  # assigned, but risky: nothing runs
    assert r.status_code == 409 and r.json()["executed"] is False and r.json()["status"] == "needs_confirmation"
    assert gw.m.hub.devices["ep_00000001"]["state"]["power"] is False
    assert gw.pub.post("/confirmations", json={"action": {"type": "run_scene", "scene": "sc_00000002"}}, headers=guest).status_code == 403
    # a token minted by someone else is ignored for a guest (stripped before it reaches the hub)
    tok = gw.pub.post("/confirmations", json={"action": {"type": "run_scene", "scene": "sc_00000002"}},
                      headers=gw.h("admin")).json()["confirmation_token"]
    r = gw.pub.post("/scenes/sc_00000002/run", json={"confirmation_token": tok}, headers=guest)
    assert r.status_code == 409
    assert gw.pub.post("/confirmations/x/retry", headers=guest).status_code in (403, 404)


def test_member_runs_only_scenes_in_their_rooms(gw):
    m = gw.h("member")
    assert gw.pub.post("/scenes/sc_00000001/run", headers=m).status_code == 200            # Bedroom
    assert gw.pub.post("/scenes/sc_00000002/run", headers=m).status_code == 403            # touches Hall
    assert gw.pub.post("/scenes/sc_00000004/run", headers=m).status_code == 403            # room-less selector: cannot verify
    assert gw.pub.post("/scenes/sc_00000004/run", headers=gw.h("admin")).status_code == 200
    assert gw.pub.post("/scenes/sc_00000009/run", headers=m).status_code == 404


def test_device_list_filters_by_role(gw):
    admin = {e["endpoint_id"] for e in gw.pub.get("/devices", headers=gw.h("admin")).json()}
    member = {e["endpoint_id"] for e in gw.pub.get("/devices", headers=gw.h("member")).json()}
    guest = {e["endpoint_id"] for e in gw.pub.get("/devices", headers=gw.h("guest")).json()}
    assert "ep_00000005" in admin and "ep_00000005" not in member      # disabled endpoint: admin only
    assert member == {"ep_00000001", "ep_00000002", "ep_00000004", "ep_00000006"} and guest == {"ep_00000001", "ep_00000002", "ep_00000006"}


def test_room_changes_apply_immediately_without_new_token(gw):
    assert gw.pub.get("/devices", headers=gw.h("guest")).json() != []
    member_h = gw.h("member")
    assert gw.pub.patch(f"/admin/users/{gw.uid('guest')}", json={"rooms": []}, headers=gw.h("admin")).status_code == 200
    assert gw.pub.get("/devices", headers=gw.h("guest")).json() == []
    assert gw.pub.patch(f"/admin/users/{gw.uid('member')}", json={"disabled": True}, headers=gw.h("admin")).status_code == 200
    assert gw.pub.get("/devices", headers=member_h).status_code == 401           # old access token dies with the account
    assert gw.pub.post("/auth/login", json={"username": "member", "password": gw.m.passwords["member"]}).status_code == 401


def test_cannot_lock_out_the_last_admin(gw):
    a = gw.uid("admin")
    assert gw.pub.patch(f"/admin/users/{a}", json={"role": "member"}, headers=gw.h("admin")).status_code == 409
    assert gw.pub.patch(f"/admin/users/{a}", json={"disabled": True}, headers=gw.h("admin")).status_code == 409
    assert gw.pub.delete(f"/admin/users/{a}", headers=gw.h("admin")).status_code == 409
    assert gw.pub.post("/admin/users", json={"username": "admin", "password": "long-enough-pw", "role": "guest"},
                       headers=gw.h("admin")).status_code == 409
