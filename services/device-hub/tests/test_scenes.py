"""Scenes: selector resolution, offline/no-capability skip, all-or-nothing confirmation, broken flagging."""
from __future__ import annotations

import pytest

from conftest import light_ep, make_announce
from device_hub.errors import HubError
from device_hub.util import action_hash, run_scene_action

U = "user:vedant"


def mk(home, name, actions, owner="u"):
    return home.hub.scenes.create_scene(name, actions, owner, "t")


def fixed(eid, cap, value):
    return {"target": {"endpoint_id": eid}, "capability": cap, "value": value}


def sel(cap, value, **t):
    return {"target": t, "capability": cap, "value": value}


def by_ep(res):
    return {(r["endpoint_id"], r["capability"]): r for r in res["results"]}


def test_fixed_targets_run_with_per_action_results(home):
    s = mk(home, "Reading", [fixed(home.ids["light"], "power", True), fixed(home.ids["light"], "brightness", 30),
                             fixed(home.ids["fan"], "speed", 2)])
    r = home.hub.scenes.run_scene(s["scene_id"])
    assert r["ok"] and r["executed"] and [x["status"] for x in r["results"]] == ["done"] * 3
    assert home.nodes["esp32-000000000001"].values["l1"] == {"power": True, "brightness": 30}
    assert home.nodes["esp32-000000000001"].cmds_seen[-1]["by"] == f"scene:{s['scene_id']}"


def test_run_by_exact_name_or_id(home):
    s = mk(home, "Night", [fixed(home.ids["light"], "power", False)])
    assert home.hub.scenes.run_scene("Night")["executed"]
    assert home.hub.scenes.run_scene(s["scene_id"])["executed"]
    assert home.hub.scenes.run_scene("night")["error"] == "unknown_scene"      # exact name only
    assert home.hub.scenes.run_scene("sc_00000000")["error"] == "unknown_scene"


def test_selector_resolution_room_type_tag(home):
    s = mk(home, "Bedroom off", [sel("power", False, room="bedroom")])
    r = home.hub.scenes.run_scene(s["scene_id"])
    got = by_ep(r)
    assert set(got) == {(home.ids["light"], "power"), (home.ids["fan"], "power"), (home.ids["temp"], "power")}
    assert got[(home.ids["light"], "power")]["status"] == "done"
    assert got[(home.ids["temp"], "power")]["status"] == "skipped_no_capability"
    # type selector
    s2 = mk(home, "Fans", [sel("speed", 4, type="fan")])
    assert [x["endpoint_id"] for x in home.hub.scenes.run_scene(s2["scene_id"])["results"]] == [home.ids["fan"]]
    # tag = admin-set tag (aliases do not count)
    home.hub.admin.patch_endpoint(home.ids["light"], {"aliases": ["reading lamp"], "tags": ["reading"]}, "t")
    s3 = mk(home, "Lamps", [sel("power", True, tag="reading")])
    assert [x["endpoint_id"] for x in home.hub.scenes.run_scene(s3["scene_id"])["results"]] == [home.ids["light"]]


def test_new_light_joins_selector_scene_without_edit(home):
    s = mk(home, "Good night", [sel("power", False, room="Bedroom", type="light")])
    assert len(home.hub.scenes.run_scene(s["scene_id"])["results"]) == 1
    home.add_node(make_announce("esp32-000000000003", [light_ep("l9")]), {"l9": ("Bed lamp", "Bedroom", None)})
    r = home.hub.scenes.run_scene(s["scene_id"])
    assert len(r["results"]) == 2 and all(x["status"] == "done" for x in r["results"])


def test_offline_endpoints_are_skipped_and_reported_not_queued(home):
    s = mk(home, "Both", [fixed(home.ids["light"], "power", True), fixed(home.ids["fan"], "power", True)])
    node1 = home.nodes["esp32-000000000001"]
    node1.t.drop()
    home.broker.flush()
    r = home.hub.scenes.run_scene(s["scene_id"])
    got = by_ep(r)
    assert got[(home.ids["light"], "power")]["status"] == "skipped_offline"
    assert got[(home.ids["fan"], "power")]["status"] == "done"
    assert r["ok"] is True
    node1.t.start()
    home.broker.flush()
    assert node1.cmds_seen == []            # the skipped command never arrives later


def test_selector_skips_endpoints_missing_the_capability(home):
    s = mk(home, "Dim all", [sel("brightness", 10, room="Bedroom")])
    got = by_ep(home.hub.scenes.run_scene(s["scene_id"]))
    assert got[(home.ids["light"], "brightness")]["status"] == "done"
    assert got[(home.ids["fan"], "brightness")]["status"] == "skipped_no_capability"


def test_device_failure_reported_per_action(home):
    home.nodes["esp32-000000000002"].drop_acks = True
    s = mk(home, "Mixed", [fixed(home.ids["light"], "power", True), fixed(home.ids["fan"], "power", True)])
    r = home.hub.scenes.run_scene(s["scene_id"])
    got = by_ep(r)
    assert got[(home.ids["fan"], "power")] == {"endpoint_id": home.ids["fan"], "capability": "power", "status": "failed",
                                               "detail": "ack_timeout"}
    assert got[(home.ids["light"], "power")]["status"] == "done" and r["ok"] is False


def test_disabled_endpoints_are_excluded_from_selectors(home):
    s = mk(home, "Speed 5", [sel("speed", 5, room="Bedroom")])
    assert by_ep(home.hub.scenes.run_scene(s["scene_id"]))[(home.ids["fan"], "speed")]["status"] == "done"
    home.hub.admin.patch_endpoint(home.ids["fan"], {"enabled": False}, "t")
    r = home.hub.scenes.run_scene(s["scene_id"])
    assert home.ids["fan"] not in [x["endpoint_id"] for x in r["results"]]


# ---------------------------------------------------------------------------------------------- confirmation
def test_risky_scene_executes_nothing_without_token(home):
    s = mk(home, "Leave", [fixed(home.ids["light"], "power", False), fixed(home.ids["lock"], "lock", "locked")])
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U)
    risky = [{"endpoint_id": home.ids["lock"], "capability": "lock", "value": "locked"}]
    assert r == {"ok": False, "executed": False, "status": "needs_confirmation", "scene_id": s["scene_id"],
                 "needs_confirmation": risky, "action_hash": action_hash(run_scene_action(s["scene_id"], risky))}
    assert home.nodes["esp32-000000000001"].cmds_seen == [] and home.nodes["esp32-000000000002"].cmds_seen == []


def test_risky_scene_with_matching_token_runs_everything(home):
    s = mk(home, "Leave", [fixed(home.ids["light"], "power", False), fixed(home.ids["lock"], "lock", "locked")])
    info = home.hub.scenes.action_for_scene(s["scene_id"])
    token = home.gw.mint(U, info["action_hash"])
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U, token=token)
    assert r["executed"] and r["ok"]
    assert len(home.nodes["esp32-000000000001"].cmds_seen) == 1 and len(home.nodes["esp32-000000000002"].cmds_seen) == 1


def test_token_for_set_state_cannot_confirm_a_scene(home):
    from device_hub.util import set_state_action
    s = mk(home, "Leave", [fixed(home.ids["lock"], "lock", "locked")])
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "locked"))
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U, token=token)
    assert r["status"] == "needs_confirmation" and not r["executed"]


def test_scene_token_binds_the_resolved_risky_actions(home):
    s = mk(home, "Locks", [sel("lock", "locked", type="lock")])
    token = home.token(U, run_scene_action(s["scene_id"], [{"endpoint_id": home.ids["lock"], "capability": "lock", "value": "locked"}]))
    # a second lock joins before the user runs it: the approved action set no longer matches -> new approval needed
    home.add_node(make_announce("esp32-000000000004", [{"local_id": "d2", "type": "lock", "capabilities": [
        {"id": "lock", "kind": "actuator", "type": "enum", "values": ["locked", "unlocked"], "boot_value": "locked"}]}]),
        {"d2": ("Back door", "Hall", "confirm")})
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U, token=token)
    assert r["status"] == "needs_confirmation" and len(r["needs_confirmation"]) == 2
    assert home.nodes["esp32-000000000002"].cmds_seen == []


def test_offline_risky_action_is_still_part_of_the_approval(home):
    s = mk(home, "Leave", [fixed(home.ids["lock"], "lock", "locked"), fixed(home.ids["light"], "power", False)])
    home.nodes["esp32-000000000002"].t.drop()
    home.broker.flush()
    assert home.hub.scenes.run_scene(s["scene_id"], user_id=U)["status"] == "needs_confirmation"
    token = home.hub.scenes.action_for_scene(s["scene_id"])["action_hash"]
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U, token=home.gw.mint(U, token))
    assert by_ep(r)[(home.ids["lock"], "lock")]["status"] == "skipped_offline"
    assert by_ep(r)[(home.ids["light"], "power")]["status"] == "done"


# ---------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("actions", [
    [],
    "nope",
    [{"target": {}, "capability": "power", "value": True}],
    [{"target": {"endpoint_id": "ep_zzzz"}, "capability": "power", "value": True}],
    [{"target": {"endpoint_id": "ep_00000000", "room": "x"}, "capability": "power", "value": True}],
    [{"target": {"floor": "1"}, "capability": "power", "value": True}],
    [{"target": {"type": "toaster"}, "capability": "power", "value": True}],
    [{"target": {"room": "Bedroom"}, "capability": "temperature", "value": 3}],
    [{"target": {"room": "Bedroom"}, "capability": "volume", "value": 3}],
    [{"target": {"room": "Bedroom"}, "capability": "brightness", "value": 500}],
    [{"target": {"room": "Bedroom"}, "capability": "power", "value": "on"}],
    [{"target": {"room": "Bedroom"}, "capability": "lock", "value": "ajar"}],
    [{"target": {"room": "Bedroom"}, "capability": "power", "value": True, "extra": 1}],
    [{"target": {"room": "Bedroom"}, "capability": "power", "value": {"a": 1}}],
    [{"target": {"room": "Bedroom"}, "capability": "power", "value": True}] * 51,
])
def test_invalid_scene_definitions_are_rejected(home, actions):
    with pytest.raises(HubError) as e:
        mk(home, "bad", actions)
    assert e.value.code == "bad_request"


def test_unknown_fixed_endpoint_rejected_and_duplicate_names(home):
    with pytest.raises(HubError) as e:
        mk(home, "x", [fixed("ep_deadbeef", "power", True)])
    assert e.value.code == "unknown_endpoint"
    mk(home, "Same", [sel("power", True, room="Hall")])
    with pytest.raises(HubError) as e:
        mk(home, "same", [sel("power", True, room="Hall")])
    assert e.value.code == "name_conflict"


# ---------------------------------------------------------------------------------------------- broken scenes
def test_forgetting_a_node_flags_dependent_scenes_broken_and_blocks_running_them(home):
    s = mk(home, "Uses fan", [fixed(home.ids["fan"], "power", True), fixed(home.ids["light"], "power", True)])
    home.hub.admin.forget_node("esp32-000000000002", confirm=True, force=False, actor="t")
    got = home.hub.scenes.get_scene(s["scene_id"])
    assert got["broken"] is True and any(home.ids["fan"] in r for r in got["broken_reasons"])
    r = home.hub.scenes.run_scene(s["scene_id"])
    assert r["error"] == "scene_broken" and r["executed"] is False
    assert home.nodes["esp32-000000000001"].cmds_seen == []         # broken scene runs nothing


def test_scene_repaired_by_editing(home):
    s = mk(home, "Uses fan", [fixed(home.ids["fan"], "power", True)])
    home.hub.admin.forget_node("esp32-000000000002", confirm=True, force=False, actor="t")
    assert home.hub.scenes.get_scene(s["scene_id"])["broken"]
    fixed_scene = home.hub.scenes.update_scene(s["scene_id"], None, [fixed(home.ids["light"], "power", True)], "t")
    assert fixed_scene["broken"] is False


def test_capability_removed_by_reannounce_breaks_scene_and_events_fire(home):
    s = mk(home, "Dim", [fixed(home.ids["light"], "brightness", 20)])
    events = []
    home.hub.bus.add_hook(lambda ev: events.append(ev["type"]))
    node = home.nodes["esp32-000000000001"]
    ann = {**node.announce, "endpoints": [{**node.announce["endpoints"][0], "capabilities": [node.announce["endpoints"][0]["capabilities"][0]]},
                                           node.announce["endpoints"][1]]}
    node.announce = ann
    node.publish_announce()
    home.broker.flush()
    assert home.hub.scenes.get_scene(s["scene_id"])["broken"] is True
    assert "scenes_changed" in events and "registry_changed" in events


def test_selector_scene_is_not_broken_when_nothing_matches(home):
    s = mk(home, "Garage", [sel("power", False, room="Garage")])
    assert s["broken"] is False
    assert home.hub.scenes.run_scene(s["scene_id"]) == {"ok": True, "executed": True, "scene": s["scene_id"],
                                                        "scene_name": "Garage", "results": []}


def test_scene_rooms_reported_for_gateway_permission_checks(home):
    s = mk(home, "Mixed", [fixed(home.ids["lock"], "lock", "locked"), sel("power", False, room="Bedroom"), sel("power", False, type="light")])
    assert s["rooms"] == ["*", "Bedroom", "Hall"]


def test_forget_usage_counts_scenes_by_fixed_and_selector(home):
    mk(home, "A", [fixed(home.ids["fan"], "power", True)])
    mk(home, "B", [sel("power", True, room="Bedroom")])
    mk(home, "C", [fixed(home.ids["lock"], "lock", "locked")])
    u = home.hub.admin.forget_usage("esp32-000000000002")
    assert u["scenes"] == 3 and u["automations"] == 0 and u["message"] == "used by 3 scenes, 0 automations"
    assert home.hub.admin.forget_usage("esp32-000000000001")["scenes"] == 1      # only the Bedroom selector scene

