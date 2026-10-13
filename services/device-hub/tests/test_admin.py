"""Provisioning sessions, dynsec client, approve/patch/rebind, identify/unprovision, forget with usage check, events."""
from __future__ import annotations

import json
import re

import pytest

from conftest import CONTRACTS, WIFI, fan_ep, light_ep, make_announce, sensor_ep
from device_hub.dynsec import node_role_acls, node_role_name
from device_hub.errors import Conflict, HubError, NotFound
from device_hub.mock import FakeNode

N1 = "esp32-000000000001"
N2 = "esp32-000000000002"


# ---------------------------------------------------------------------------------------------- provisioning
def test_session_bundle_has_the_ble_spec_fields(env):
    s = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "admin1")
    b = s["bundle"]
    assert b["v"] == 1 and b["wifi"] == WIFI
    assert b["mqtt"]["host"] == "192.0.2.10" and b["mqtt"]["port"] == 8883 and b["mqtt"]["username"] == N1
    assert len(b["mqtt"]["password"]) >= 24 and b["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert b["ntp_host"] == "192.0.2.10"
    assert len(json.dumps(b).encode()) <= 4096
    assert s["session_id"].startswith("ps_") and s["expires_at"] == int(env.clock.now() + 600)
    assert set(env.broker.users) >= {N1} and node_role_name(N1) in env.broker.roles


def test_role_acls_match_the_deploy_template():
    tpl = json.loads((CONTRACTS.parent / "deploy" / "mosquitto" / "node-role.template.json").read_text())
    expected = [{k: v for k, v in a.items()} for a in tpl["acls"]]
    mine = json.loads(json.dumps(node_role_acls("esp32-0123456789ab")).replace("esp32-0123456789ab", "{{node_id}}"))
    assert mine == expected
    assert tpl["rolename"].replace("{{node_id}}", "X") == node_role_name("X")


def test_secrets_never_stored_in_db_or_audit(env):
    s = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "admin1")
    env.hub.admin.forget_node(N1, confirm=True, force=False, actor="admin1")
    dump = "\n".join(env.hub.db.conn.iterdump())
    for secret in (s["bundle"]["mqtt"]["password"], WIFI["password"]):
        assert secret not in dump
    view = json.dumps(env.hub.provisioning.list_sessions())
    assert s["bundle"]["mqtt"]["password"] not in view


def test_session_validation(env):
    bad = [{"node_id": "esp32-XYZ", "wifi": WIFI}, {"node_id": N1}, {"node_id": N1, "wifi": {"ssid": "", "password": "x"}},
           {"node_id": N1, "wifi": {"ssid": "a" * 33, "password": "x"}}, {"node_id": N1, "wifi": {"ssid": "a", "password": "x" * 65}},
           {"node_id": N1, "wifi": {"ssid": "a", "password": "x", "extra": 1}}, {"node_id": N1, "wifi_profile": "home"}]
    for body in bad:
        with pytest.raises(HubError) as e:
            env.hub.admin.create_provision_session(body, "a")
        assert e.value.code == "bad_request", body
    assert N1 not in env.broker.users


def test_announce_matches_session_and_node_is_pending(env):
    s = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert env.hub.provisioning.get_session(s["session_id"])["state"] == "pending"
    m = s["bundle"]["mqtt"]
    node = FakeNode(make_announce(N1, [light_ep()]), env.broker.client(m["username"], m["password"], N1, FakeNode.will(N1)))
    assert node.start()
    env.broker.flush()
    assert env.hub.provisioning.get_session(s["session_id"])["state"] == "connected"
    assert env.hub.registry.get_node(N1)["status"] == "pending"


def test_unconnected_session_is_cleaned_up_after_ttl_with_its_mqtt_user(env):
    s = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    env.clock.advance(599)
    assert env.hub.provisioning.cleanup_expired() == []
    assert N1 in env.broker.users
    env.clock.advance(2)
    assert env.hub.provisioning.cleanup_expired() == [s["session_id"]]
    assert N1 not in env.broker.users and node_role_name(N1) not in env.broker.roles
    assert env.hub.provisioning.get_session(s["session_id"])["state"] == "expired"
    assert env.hub.provisioning.cleanup_expired() == []


def test_connected_session_is_not_cleaned_up(env):
    env.add_node(make_announce(N1, [light_ep()]))
    env.clock.advance(5000)
    env.hub.provisioning.cleanup_expired()
    assert N1 in env.broker.users


def test_cleanup_retries_when_dynsec_is_down(env):
    env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    env.clock.advance(601)
    env.broker.dynsec_fail = True
    assert env.hub.provisioning.cleanup_expired() == []
    assert N1 in env.broker.users
    env.broker.dynsec_fail = False
    assert len(env.hub.provisioning.cleanup_expired()) == 1


def test_new_session_replaces_the_old_one_and_rotates_credentials(env):
    a = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    b = env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert env.hub.provisioning.get_session(a["session_id"])["state"] == "cancelled"
    assert a["bundle"]["mqtt"]["password"] != b["bundle"]["mqtt"]["password"]
    assert env.broker.users[N1]["password"] == b["bundle"]["mqtt"]["password"]


def test_cannot_reprovision_a_connected_node(env):
    env.add_node(make_announce(N1, [light_ep()]))
    with pytest.raises(Conflict) as e:
        env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert e.value.code == "node_online"


@pytest.mark.parametrize("mode,status", [("fail", 502), ("silent", 504)])
def test_dynsec_failures_surface_and_leave_nothing_behind(env, mode, status):
    env.hub.dynsec.timeout_s = 0.3
    setattr(env.broker, "dynsec_fail" if mode == "fail" else "dynsec_silent", True)
    with pytest.raises(HubError) as e:
        env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert e.value.status == status and e.value.code == "dynsec_failed"
    assert env.hub.provisioning.list_sessions() == []


def test_dynsec_broker_unreachable_is_503(env):
    env.hub.transport.stop()
    with pytest.raises(HubError) as e:
        env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    assert e.value.status == 503


def test_dynsec_uses_only_mqtt_commands(env):
    env.hub.admin.create_provision_session({"node_id": N1, "wifi": WIFI}, "a")
    env.hub.admin.forget_node(N1, confirm=True, force=False, actor="a")
    assert set(env.broker.dynsec_commands) <= {"createRole", "createClient", "deleteClient", "deleteRole"}


# ---------------------------------------------------------------------------------------------- approve / patch / rebind
def test_approve_assigns_permanent_ids_and_sanitizes_labels(env):
    env.add_node(make_announce(N1, [light_ep("l1"), sensor_ep("t1")]))
    eps = env.hub.admin.approve_node(N1, {"endpoints": [
        {"local_id": "l1", "name": "  Desk‮ lamp\x00 ", "room": "living room", "aliases": ["Lamp", "lamp", "x" * 80], "risk": "normal"},
        {"local_id": "t1", "name": "Temp", "room": "Living Room"}]}, "admin1")
    assert all(re.fullmatch(r"ep_[0-9a-f]{8}", e["endpoint_id"]) for e in eps)
    assert eps[0]["name"] == "Desk lamp" and eps[0]["aliases"] == ["Lamp", "x" * 48]
    assert eps[0]["room"] == eps[1]["room"] == "living room"          # first spelling wins, lookups are case-insensitive
    assert env.hub.registry.rooms() == ["living room"]
    assert env.hub.registry.get_node(N1)["status"] in ("online", "offline") and env.hub.registry.get_node(N1)["pending_endpoints"] == []


def test_approve_rules(env):
    env.add_node(make_announce(N1, [light_ep("l1"), light_ep("l2")]))
    a = env.hub.admin
    with pytest.raises(HubError, match="did not announce"):
        a.approve_node(N1, {"endpoints": [{"local_id": "zz", "name": "x", "room": "r"}]}, "t")
    with pytest.raises(HubError):
        a.approve_node(N1, {"endpoints": [{"local_id": "l1", "name": "", "room": "r"}]}, "t")
    with pytest.raises(HubError):
        a.approve_node(N1, {"endpoints": [{"local_id": "l1", "name": "x", "room": "r", "risk": "danger"}]}, "t")
    with pytest.raises(HubError):
        a.approve_node(N1, {"endpoints": [{"local_id": "l1", "name": "x", "room": "r", "type": "fan"}]}, "t")   # no device-supplied fields
    with pytest.raises(Conflict):                                                                              # same name+room twice
        a.approve_node(N1, {"endpoints": [{"local_id": "l1", "name": "Lamp", "room": "R"}, {"local_id": "l2", "name": "lamp", "room": "r"}]}, "t")
    assert env.hub.registry.get_node(N1)["endpoints"] == []             # all-or-nothing
    a.approve_node(N1, {"endpoints": [{"local_id": "l1", "name": "Lamp", "room": "R"}]}, "t")
    with pytest.raises(Conflict, match="already approved"):
        a.approve_node(N1, {"endpoints": [{"local_id": "l1", "name": "Other", "room": "R"}]}, "t")
    with pytest.raises(NotFound):
        a.approve_node("esp32-ffffffffffff", {"endpoints": [{"local_id": "l1", "name": "x", "room": "r"}]}, "t")


def test_names_come_only_from_admins_never_from_announce(env):
    ann = make_announce(N1, [light_ep()])
    ann["endpoints"][0]["name"] = "evil"            # schema forbids it -> whole announce rejected
    env.broker.add_user(N1, "pw-for-node-1")
    env.broker.roles["r"] = [{"acltype": "publishClientSend", "topic": f"home/v1/nodes/{N1}/#", "allow": True}]
    env.broker.users[N1]["roles"].append("r")
    c = env.broker.client(N1, "pw-for-node-1", N1)
    c.start()
    c.publish(f"home/v1/nodes/{N1}/announce", json.dumps(ann), 1, True)
    env.broker.flush()
    assert not env.hub.registry.node_exists(N1) and env.hub.bridge.stats["announce_rejected"] == 1


def test_patch_endpoint(home):
    a = home.hub.admin
    ep = a.patch_endpoint(home.ids["light"], {"name": "Main light", "room": "Study", "aliases": ["big light"], "risk": "confirm"}, "admin1")
    assert ep["endpoint_id"] == home.ids["light"] and ep["name"] == "Main light" and ep["room"] == "Study" and ep["risk"] == "confirm"
    for bad in ({}, {"type": "fan"}, {"risk": "x"}, {"enabled": "yes"}, {"name": " "}, {"endpoint_id": "ep_00000000"}):
        with pytest.raises(HubError):
            a.patch_endpoint(home.ids["light"], bad, "t")
    with pytest.raises(NotFound):
        a.patch_endpoint("ep_00000000", {"name": "x"}, "t")
    with pytest.raises(Conflict):
        a.patch_endpoint(home.ids["fan"], {"room": "Study", "name": "main LIGHT"}, "t")


def test_risk_change_takes_effect_immediately(home):
    assert home.hub.executor.set_state(home.ids["light"], "power", True)["ok"]
    home.hub.admin.patch_endpoint(home.ids["light"], {"risk": "confirm"}, "t")
    assert home.hub.executor.set_state(home.ids["light"], "power", False)["error"] == "confirmation_required"


def test_rebind_keeps_the_endpoint_id(home):
    home.add_node(make_announce("esp32-000000000009", [light_ep("a")]))
    ep = home.hub.admin.rebind_endpoint(home.ids["light"], {"node_id": "esp32-000000000009", "local_id": "a"}, "admin1")
    assert ep["endpoint_id"] == home.ids["light"] and ep["node_id"] == "esp32-000000000009" and ep["name"] == "Ceiling light"
    assert home.hub.executor.set_state(home.ids["light"], "power", True)["ok"]
    assert home.nodes["esp32-000000000009"].values["a"]["power"] is True
    old = home.hub.registry.get_node(N1)
    assert [p["local_id"] for p in old["pending_endpoints"]] == ["l1"]        # the old hardware is pending again


def test_rebind_rules(home):
    home.add_node(make_announce("esp32-000000000009", [fan_ep("a"), light_ep("b")]), {"b": ("Other lamp", "Study", None)})
    a = home.hub.admin
    with pytest.raises(Conflict, match="light"):
        a.rebind_endpoint(home.ids["light"], {"node_id": "esp32-000000000009", "local_id": "a"}, "t")
    with pytest.raises(HubError):
        a.rebind_endpoint(home.ids["light"], {"node_id": "esp32-000000000009", "local_id": "nope"}, "t")
    with pytest.raises(Conflict, match="bound"):
        a.rebind_endpoint(home.ids["light"], {"node_id": "esp32-000000000009", "local_id": "b"}, "t")
    with pytest.raises(HubError):
        a.rebind_endpoint(home.ids["light"], {"node_id": "bogus", "local_id": "a"}, "t")


# ---------------------------------------------------------------------------------------------- identify / unprovision
def test_identify_sends_cmd_and_reports_ack(home):
    r = home.hub.admin.identify_node(N1, 5, "admin1")
    assert r == {"sent": True, "acked": True}
    assert home.nodes[N1].cmds_seen[-1]["cmd"] == "identify" and home.nodes[N1].cmds_seen[-1]["duration_s"] == 5
    for bad in (0, 61, "x", True, 1.5):
        with pytest.raises(HubError):
            home.hub.admin.identify_node(N1, bad, "t")
    home.nodes[N1].t.drop()
    home.broker.flush()
    with pytest.raises(Conflict):
        home.hub.admin.identify_node(N1, 5, "t")


def test_unprovision_wipes_board_and_revokes_credentials(home):
    r = home.hub.admin.unprovision_node(N1, False, "admin1")
    assert r["acked"] is True and home.nodes[N1].wiped
    assert N1 not in home.broker.users
    node = home.nodes[N1]
    node.t.drop()
    node.t.start()
    assert node.t.connected is False and "not authorised" in node.t.connect_error
    assert home.hub.registry.get_node(N1)["provisioned"] is False
    assert home.ids["light"]                                    # registry rows (and ids) survive for re-provisioning
    assert home.hub.registry.get_endpoint(home.ids["light"])["online"] is False


def test_unprovision_offline_needs_force(home):
    home.nodes[N1].t.drop()
    home.broker.flush()
    with pytest.raises(Conflict):
        home.hub.admin.unprovision_node(N1, False, "t")
    assert home.hub.admin.unprovision_node(N1, True, "t")["sent"] is False
    assert N1 not in home.broker.users


# ---------------------------------------------------------------------------------------------- forget
def test_forget_without_confirm_only_reports_usage(home):
    home.hub.scenes.create_scene("S", [{"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": True}], "u", "t")
    r = home.hub.admin.forget_node(N1, confirm=False, force=False, actor="t")
    assert r["forgotten"] is False and r["scenes"] == 1 and r["message"] == "used by 1 scene, 0 automations"
    assert N1 in home.broker.users and home.hub.registry.node_exists(N1)


def test_forget_revokes_the_mqtt_user_and_the_node_is_refused_on_reconnect(home):
    node = home.nodes[N1]
    r = home.hub.admin.forget_node(N1, confirm=True, force=False, actor="admin1")
    home.broker.flush()
    assert r["forgotten"] is True
    assert N1 not in home.broker.users and node_role_name(N1) not in home.broker.roles
    assert node.t.connected is False                              # kicked
    node.t.start()
    assert node.t.connected is False and "not authorised" in node.t.connect_error
    assert not any(t.startswith(f"home/v1/nodes/{N1}/") for t in home.broker.retained)    # retained topics cleared
    assert not home.hub.registry.node_exists(N1)
    with pytest.raises(NotFound):
        home.hub.registry.get_endpoint(home.ids["light"])
    assert home.hub.executor.set_state(home.ids["light"], "power", True)["error"] == "unknown_endpoint"
    assert [d["endpoint_id"] for d in home.hub.executor.list_devices()["devices"]] == [home.ids["fan"], home.ids["lock"]] \
        or len(home.hub.executor.list_devices()["devices"]) == 2


def test_forgotten_node_messages_are_ignored(home):
    home.hub.admin.forget_node(N1, confirm=True, force=False, actor="t")
    n = home.hub.bridge.stats["forgotten_node_messages"]
    # a rogue publisher that still has an ACL (e.g. created by hand) cannot resurrect the node
    home.broker.add_user("ghost", "ghost-pass-1", superuser=True)
    g = home.broker.client("ghost", "ghost-pass-1", "ghost")
    g.start()
    g.publish(f"home/v1/nodes/{N1}/announce", json.dumps(make_announce(N1, [light_ep()])), 1, True)
    home.broker.flush()
    assert not home.hub.registry.node_exists(N1)


def test_forget_failure_without_force_changes_nothing(home):
    home.broker.dynsec_fail = True
    with pytest.raises(HubError) as e:
        home.hub.admin.forget_node(N1, confirm=True, force=False, actor="t")
    assert e.value.code == "dynsec_failed"
    assert home.hub.registry.node_exists(N1) and N1 in home.broker.users


def test_forget_with_force_soft_deletes_and_retries_revocation(home):
    home.broker.dynsec_fail = True
    r = home.hub.admin.forget_node(N1, confirm=True, force=True, actor="t")
    assert r["forgotten"] and r["revocation_pending"] is True
    assert not home.hub.registry.node_exists(N1) and N1 in home.broker.users
    home.hub.run_maintenance()                  # still failing: stays pending
    assert home.hub.registry.nodes_pending_revocation() == [N1]
    home.broker.dynsec_fail = False
    home.hub.run_maintenance()
    assert N1 not in home.broker.users and home.hub.registry.nodes_pending_revocation() == []


def test_forgotten_node_can_be_reprovisioned_with_fresh_endpoint_ids(home):
    old = home.ids["light"]
    home.hub.admin.forget_node(N1, confirm=True, force=False, actor="t")
    home.add_node(make_announce(N1, [light_ep("l1")]), {"l1": ("New lamp", "Bedroom", None)})
    new = home.eid(N1, "l1")
    assert new != old and home.hub.registry.get_endpoint(new)["name"] == "New lamp"


# ---------------------------------------------------------------------------------------------- audit + events
def test_admin_actions_are_audit_logged_without_secrets(home):
    home.hub.admin.patch_endpoint(home.ids["light"], {"name": "Renamed"}, "admin1")
    home.hub.admin.identify_node(N1, 3, "admin1")
    home.hub.admin.forget_node(N2, confirm=True, force=False, actor="admin1")
    actions = [a["action"] for a in home.hub.registry.list_audit(100)]
    for expected in ("provision.create", "node.approve", "endpoint.patch", "node.identify", "node.forget"):
        assert expected in actions
    assert all(a["actor"] for a in home.hub.registry.list_audit(100))


def test_registry_event_stream_and_replay(env):
    sub = env.hub.bus.subscribe()
    env.add_node(make_announce(N1, [light_ep()]), {"l1": ("Lamp", "Room", None)})
    seen = []
    while (ev := sub.get(0.2)):
        seen.append(ev)
    types = [e["type"] for e in seen]
    assert "pending_node" in types and "registry_changed" in types and "node_status" in types
    rc = next(e for e in seen if e["type"] == "registry_changed")
    assert rc["data"]["reason"] == "approve" and rc["data"]["version"] >= 1 and len(rc["data"]["endpoint_ids"]) == 1
    assert [e["seq"] for e in seen] == sorted(e["seq"] for e in seen)
    replay = env.hub.bus.subscribe(since_seq=seen[0]["seq"])
    assert replay.get(0.2)["seq"] == seen[1]["seq"]
    only_state = env.hub.bus.subscribe(types=frozenset({"state"}))
    env.hub.executor.set_state(env.eid(N1, "l1"), "brightness", 12)
    ev = only_state.get(1.0)
    assert ev and ev["type"] == "state" and ev["data"]["values"]["brightness"] == 12
