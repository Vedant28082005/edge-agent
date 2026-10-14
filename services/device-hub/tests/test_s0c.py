"""Contract changes S0b/S0c: settle_s + done/moving + settle events, idempotent dynsec, tags, reconnect lifecycle,
new response fields and error codes."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from conftest import WIFI, curtain_ep, light_ep, make_announce, sensor_ep
from device_hub.db import Database
from device_hub.dynsec import node_role_acls, node_role_name
from device_hub.errors import HubError
from device_hub.mock import FakeNode

N1 = "esp32-000000000001"
N3 = "esp32-000000000003"
U = "user:vedant"


@pytest.fixture
def curtain(home):
    node = home.add_node(make_announce(N3, [curtain_ep("c1", settle_s=2)]), {"c1": ("Curtain", "Bedroom", None)})
    home.ids["curtain"] = home.eid(N3, "c1")
    home.events = []
    home.hub.bus.add_hook(lambda ev: home.events.append(ev) if ev["type"].startswith("action_") else None)
    return home, node


def wait(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------------------------- settle_s
def test_settle_s_is_stored_in_the_capability(curtain):
    home, _ = curtain
    cap = home.hub.registry.get_endpoint(home.ids["curtain"])["capabilities"][0]
    assert cap["settle_s"] == 2 and cap["id"] == "position"


def test_motion_returns_moving_then_action_settled(curtain):
    home, node = curtain
    t0 = time.time()
    r = home.hub.executor.set_state(home.ids["curtain"], "position", 80, call_id="call_7")
    assert time.time() - t0 < 1.0, "the tool call must not block for settle_s"
    assert r == {"ok": True, "status": "moving", "verified": False, "endpoint_id": home.ids["curtain"],
                 "capability": "position", "value": 80}
    assert wait(lambda: any(e["type"] == "action_settled" for e in home.events))
    ev = next(e for e in home.events if e["type"] == "action_settled")["data"]
    assert ev["endpoint_id"] == home.ids["curtain"] and ev["capability"] == "position" and ev["value"] == 80
    assert ev["observed"] == 80 and ev["call_id"] == "call_7" and ev["elapsed_ms"] >= 0
    assert [e["type"] for e in home.events] == ["action_settled"]


def test_ack_values_are_the_target_not_the_position(curtain):
    """The node acks the accepted target immediately while state still shows the old position."""
    home, node = curtain
    node.motion_delay = 0.4
    home.hub.executor.set_state(home.ids["curtain"], "position", 60)
    assert home.hub.executor.get_state(endpoint_id=home.ids["curtain"])["states"][0]["values"]["position"] == 0
    assert wait(lambda: any(e["type"] == "action_settled" for e in home.events))


def test_tolerance_plus_minus_two_for_position(curtain):
    home, node = curtain
    node.stop_short = 2
    home.hub.executor.set_state(home.ids["curtain"], "position", 50)
    assert wait(lambda: home.events) and home.events[0]["type"] == "action_settled" and home.events[0]["data"]["observed"] == 48
    home.events.clear()
    node.stop_short = 3
    home.hub.executor.set_state(home.ids["curtain"], "position", 90)
    assert wait(lambda: home.events, 6) and home.events[0]["type"] == "action_settle_timeout"


def test_action_settle_timeout_with_observed_value(curtain):
    home, node = curtain
    node.stall = True
    home.hub.registry.get_endpoint(home.ids["curtain"])
    # shrink settle_s so the test is quick: re-announce with settle_s=1
    node.announce = make_announce(N3, [curtain_ep("c1", settle_s=1)])
    node.publish_announce()
    home.broker.flush()
    r = home.hub.executor.set_state(home.ids["curtain"], "position", 70)
    assert r["status"] == "moving"
    assert wait(lambda: any(e["type"] == "action_settle_timeout" for e in home.events), 4)
    d = next(e for e in home.events if e["type"] == "action_settle_timeout")["data"]
    assert d["settle_s"] == 1 and d["observed"] is None and d["value"] == 70
    assert not any(e["type"] == "action_settled" for e in home.events)


def test_a_newer_command_supersedes_the_older_watcher(curtain):
    home, node = curtain
    node.stall = True
    home.hub.executor.set_state(home.ids["curtain"], "position", 30)
    node.stall = False
    node.motion_delay = 0.05
    home.hub.executor.set_state(home.ids["curtain"], "position", 55)
    assert wait(lambda: home.events)
    time.sleep(2.3)                       # longer than settle_s: the first watcher must stay silent
    assert [(e["type"], e["data"]["value"]) for e in home.events] == [("action_settled", 55)]


def test_run_scene_reports_done_and_moving_per_action(curtain):
    home, node = curtain
    home.hub.scenes.create_scene("Morning", [
        {"target": {"endpoint_id": home.ids["curtain"]}, "capability": "position", "value": 100},
        {"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": True}], "u", "t")
    r = home.hub.scenes.run_scene("Morning")
    st = {x["capability"]: x["status"] for x in r["results"]}
    assert st == {"position": "moving", "power": "done"} and r["ok"] is True
    assert wait(lambda: any(e["type"] == "action_settled" for e in home.events))


def test_announce_validation_of_settle_s():
    from device_hub.contracts import AnnounceRejected, Contracts
    c = Contracts()
    ok = make_announce(N3, [curtain_ep("c1", 45)])
    assert c.parse_announce(json.dumps(ok).encode(), N3)
    for bad in (curtain_ep("c1", 0), curtain_ep("c1", 121)):
        with pytest.raises(AnnounceRejected):
            c.parse_announce(json.dumps(make_announce(N3, [bad])).encode(), N3)
    no_settle = curtain_ep("c1")
    del no_settle["capabilities"][0]["settle_s"]
    with pytest.raises(AnnounceRejected, match="settle_s"):
        c.parse_announce(json.dumps(make_announce(N3, [no_settle])).encode(), N3)


# ---------------------------------------------------------------------------------------------- idempotent dynsec
def test_existing_client_gets_the_new_password_and_role_ensured(env):
    b = env.broker
    a = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    old_pw = a["bundle"]["mqtt"]["password"]
    # damage: the role lost its ACLs and the client lost the role (e.g. a half-finished earlier attempt)
    b.roles[node_role_name(N1)] = []
    b.users[N1]["roles"] = []
    n_before = len(b.dynsec_commands)
    c = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    new_pw = c["bundle"]["mqtt"]["password"]
    assert new_pw != old_pw and b.users[N1]["password"] == new_pw           # never keeps the old password
    assert b.users[N1]["roles"] == [node_role_name(N1)]                      # role ensured
    assert b.roles[node_role_name(N1)] == node_role_acls(N1)                 # ACLs ensured
    assert "deleteClient" not in b.dynsec_commands[n_before:]                # set, not delete+recreate
    assert "setClientPassword" in b.dynsec_commands[n_before:]
    # the new credentials work, the old do not
    assert b.client(N1, new_pw, N1).start() is None
    refused = b.client(N1, old_pw, N1)
    refused.start()
    assert refused.connected is False


def test_user_exists_but_role_missing(env):
    b = env.broker
    b.add_user(N1, "stale-password-1")                    # client created by hand, no role at all
    s = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert b.users[N1]["password"] == s["bundle"]["mqtt"]["password"] and b.users[N1]["roles"] == [node_role_name(N1)]


def test_failed_creation_rolls_back_only_what_it_created(env):
    b = env.broker
    orig = b._dynsec_one

    def flaky(name, cmd):
        if name == "addClientRole":
            return "boom"
        return orig(name, cmd)

    b._dynsec_one = flaky
    with pytest.raises(HubError):
        env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert N1 not in b.users and node_role_name(N1) not in b.roles


# ---------------------------------------------------------------------------------------------- connect lifecycle
def test_every_connect_runs_the_lifecycle_and_identical_announce_is_a_noop(home):
    node = home.nodes[N1]
    seq = home.hub.bus.last_seq
    version = home.hub.registry.version
    assert node.reconnect()
    home.broker.flush()
    assert home.hub.registry.get_endpoint(home.ids["light"])["online"] is True
    assert home.hub.registry.version == version, "an identical announce must not change the registry"
    events = [e["type"] for e in home.hub.bus.subscribe(since_seq=seq).q.queue]
    assert "registry_changed" not in events and "pending_node" not in events
    assert home.hub.executor.set_state(home.ids["light"], "power", True)["ok"]       # cmd subscription restored


def test_node_publishes_in_contract_order_on_connect(env):
    order = []
    env.hub.transport.add_message_handler(lambda t, p, r: order.append(t.rsplit("/", 1)[1]) if "/nodes/" in t else None)
    env.add_node(make_announce(N1, [light_ep()]))
    assert order[:3] == ["status", "announce", "state"]


def test_node_dedup_ring_keeps_only_16(home):
    node = home.nodes[N1]
    for i in range(20):
        home.hub.executor.set_state(home.ids["light"], "brightness", i)
    assert len(node._acks) == 16


# ---------------------------------------------------------------------------------------------- tags
def test_tags_admin_set_validated_and_listed(home):
    a = home.hub.admin
    ep = a.patch_endpoint(home.ids["light"], {"tags": ["night", "reading_lamp", "x-1"]}, "admin")
    assert ep["tags"] == ["night", "reading_lamp", "x-1"]
    for bad in (["Night"], ["a b"], ["x" * 25], [""], ["a"] * 2, [str(i) for i in range(9)], "night", [1]):
        with pytest.raises(HubError) as e:
            a.patch_endpoint(home.ids["light"], {"tags": bad}, "admin")
        assert e.value.code == "bad_request", bad
    assert a.patch_endpoint(home.ids["light"], {"tags": []}, "admin")["tags"] == []
    d = {x["endpoint_id"]: x for x in home.hub.executor.list_devices()["devices"]}
    assert d[home.ids["fan"]]["tags"] == []


def test_tags_at_approve_time(env):
    env.add_node(make_announce(N1, [light_ep("a"), light_ep("b")]))
    eps = env.hub.admin.approve_node(N1, {"endpoints": [
        {"local_id": "a", "name": "A", "room": "R", "tags": ["night"]}, {"local_id": "b", "name": "B", "room": "R"}]}, "x")
    assert eps[0]["tags"] == ["night"] and eps[1]["tags"] == []
    with pytest.raises(HubError):
        env.hub.admin.approve_node("esp32-000000000009", {"endpoints": [{"local_id": "a", "name": "A", "room": "R", "tags": ["BAD"]}]}, "x")


def test_device_cannot_set_tags_via_announce(env):
    from device_hub.contracts import AnnounceRejected, Contracts
    ann = make_announce(N1, [light_ep()])
    ann["endpoints"][0]["tags"] = ["night"]
    with pytest.raises(AnnounceRejected):
        Contracts().parse_announce(json.dumps(ann).encode(), N1)


def test_tag_selectors_use_tags_not_aliases_and_combine_with_and(home):
    a = home.hub.admin
    a.patch_endpoint(home.ids["light"], {"tags": ["night"], "aliases": ["sleepy"]}, "t")
    a.patch_endpoint(home.ids["fan"], {"tags": ["night"]}, "t")
    a.patch_endpoint(home.ids["lock"], {"tags": ["night"]}, "t")

    def targets(**sel):
        s = home.hub.scenes.create_scene(f"s{len(sel)}{sorted(sel.values())}", [{"target": sel, "capability": "power", "value": True}], "u", "t")
        return {r.ep["endpoint_id"] for r in home.hub.scenes.resolve(s)}

    assert targets(tag="night") == {home.ids["light"], home.ids["fan"], home.ids["lock"]}
    assert targets(tag="night", room="Bedroom") == {home.ids["light"], home.ids["fan"]}
    assert targets(tag="night", room="Bedroom", type="fan") == {home.ids["fan"]}
    assert targets(tag="sleepy") == set(), "aliases no longer match tag selectors"


def test_tag_selector_format_is_validated(home):
    for bad in ("Night", "a b", "x" * 25, ""):
        with pytest.raises(HubError):
            home.hub.scenes.create_scene("bad", [{"target": {"tag": bad}, "capability": "power", "value": True}], "u", "t")


def test_v1_database_gets_the_tags_column(tmp_path):
    path = str(tmp_path / "old.sqlite")
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE endpoints (endpoint_id TEXT PRIMARY KEY, node_id TEXT, local_id TEXT, type TEXT, name TEXT, "
                    "room TEXT, aliases TEXT, risk TEXT, enabled INT, present INT, deleted INT, created REAL, updated REAL);")
    c.close()
    db = Database(path)
    assert "tags" in {r["name"] for r in db.query("PRAGMA table_info(endpoints)")}
    db.close()


# ---------------------------------------------------------------------------------------------- new codes
def test_unknown_room_and_other_codes(home):
    ex = home.hub.executor
    assert ex.list_devices(room="Garage") == {"ok": False, "error": "unknown_room", "devices": []}
    home.hub.admin.patch_endpoint(home.ids["fan"], {"room": "Study"}, "t")
    home.hub.admin.patch_endpoint(home.ids["light"], {"room": "Study"}, "t")
    home.hub.admin.patch_endpoint(home.ids["temp"], {"room": "Study"}, "t")
    assert ex.list_devices(room="Hall")["devices"] and "Bedroom" in home.hub.registry.rooms()
    assert ex.list_devices(room="Bedroom") == {"devices": []}                  # existing room, no endpoints: empty, not an error
    assert ex.get_state(room="Bedroom") == {"states": []}
    assert ex.get_state(room="Garage")["error"] == "unknown_room"
    assert ex.read_sensor(home.ids["temp"], "power")["error"] == "unknown_capability"
    assert ex.read_sensor(home.ids["light"], "power")["error"] == "not_sensor"
    home.hub.admin.patch_endpoint(home.ids["temp"], {"enabled": False}, "t")
    assert ex.read_sensor(home.ids["temp"], "temperature")["error"] == "disabled"
    assert ex.get_state(endpoint_id=home.ids["temp"])["error"] == "unknown_endpoint"
    assert home.hub.scenes.run_scene("nope") == {"ok": False, "executed": False, "error": "unknown_scene"}


# ---------------------------------------------------------------------------------------------- S0e
def test_run_scene_needs_confirmation_carries_scene_id_canonical_actions_and_hash(home):
    from device_hub.util import action_hash, run_scene_action
    s = home.hub.scenes.create_scene("Leave", [
        {"target": {"endpoint_id": home.ids["lock"]}, "capability": "lock", "value": "locked"},
        {"target": {"type": "lock"}, "capability": "lock", "value": "locked"},          # duplicate of the first, collapses
        {"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": False}], "u", "t")
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U)
    risky = [{"endpoint_id": home.ids["lock"], "capability": "lock", "value": "locked"}]
    assert r == {"ok": False, "executed": False, "status": "needs_confirmation", "scene_id": s["scene_id"],
                 "needs_confirmation": risky, "action_hash": action_hash(run_scene_action(s["scene_id"], risky))}
    # the hash is exactly what the confirmation lookup gives the gateway, and a token minted for it runs the scene
    assert r["action_hash"] == home.hub.scenes.action_for_scene(s["scene_id"])["action_hash"]
    by_name = home.hub.scenes.run_scene("Leave", user_id=U)
    assert by_name["scene_id"] == s["scene_id"]                                           # permanent id even when run by name
    tok = home.gw.mint(U, r["action_hash"])
    assert home.hub.scenes.run_scene(s["scene_id"], user_id=U, token=tok)["executed"] is True


def test_needs_confirmation_actions_are_sorted_by_endpoint_then_capability(home):
    from device_hub.util import action_hash
    home.add_node(make_announce("esp32-00000000000a", [{"local_id": "d2", "type": "lock", "capabilities": [
        {"id": "lock", "kind": "actuator", "type": "enum", "values": ["locked", "unlocked"], "boot_value": "locked"}]}]),
        {"d2": ("Back door", "Hall", "confirm")})
    s = home.hub.scenes.create_scene("Locks", [{"target": {"type": "lock"}, "capability": "lock", "value": "locked"}], "u", "t")
    r = home.hub.scenes.run_scene(s["scene_id"], user_id=U)
    ids = [a["endpoint_id"] for a in r["needs_confirmation"]]
    assert ids == sorted(ids) and len(ids) == 2
    assert all(set(a) == {"endpoint_id", "capability", "value"} for a in r["needs_confirmation"])
    assert len(r["action_hash"]) == 64 and r["action_hash"] == action_hash(
        {"actions": r["needs_confirmation"], "scene_id": s["scene_id"], "type": "run_scene"})
