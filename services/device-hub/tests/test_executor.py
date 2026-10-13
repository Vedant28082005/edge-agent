"""set_state: validation, ack timeout, dedup, readback, confirmation tokens; read_sensor; get_state; list_devices."""
from __future__ import annotations

import json

import pytest

from conftest import CONTRACTS, load_json
from device_hub.util import action_hash, canonical_json, run_scene_action, set_state_action

U = "user:vedant"


def set_(home, who, cap, value, **kw):
    return home.hub.executor.set_state(home.ids[who], cap, value, **kw)


def test_set_state_round_trip_with_ack_and_readback(home):
    r = set_(home, "light", "brightness", 70)
    assert r == {"ok": True, "status": "done", "verified": True, "endpoint_id": home.ids["light"],
                 "capability": "brightness", "value": 70}
    node = home.nodes["esp32-000000000001"]
    assert node.values["l1"]["brightness"] == 70
    assert node.cmds_seen[-1]["by"] == "agent"
    state = home.hub.executor.get_state(endpoint_id=home.ids["light"])["states"][0]
    assert state["values"]["brightness"] == 70 and state["online"] is True


def test_by_field_carries_the_user(home):
    set_(home, "light", "power", True, user_id="user:vedant")
    assert home.nodes["esp32-000000000001"].cmds_seen[-1]["by"] == "user:vedant"
    set_(home, "light", "power", False, user_id="weird name!")
    assert home.nodes["esp32-000000000001"].cmds_seen[-1]["by"] == "user:weirdname"


@pytest.mark.parametrize("ep,cap,value,err", [
    ("light", "brightness", 101, "out_of_range"),
    ("light", "brightness", -1, "out_of_range"),
    ("light", "brightness", "50", "bad_type"),
    ("light", "brightness", 50.5, "bad_type"),
    ("light", "brightness", True, "bad_type"),
    ("light", "power", 1, "bad_type"),
    ("light", "power", "on", "bad_type"),
    ("fan", "speed", 0, "out_of_range"),
    ("fan", "speed", 6, "out_of_range"),
    ("fan", "mode", "ludicrous", "out_of_range"),
    ("fan", "mode", 3, "bad_type"),
    ("light", "nonsense", 1, "unknown_capability"),
    ("light", "volume", 1, "unknown_capability"),
    ("temp", "temperature", 20, "not_actuator"),
])
def test_validation_codes(home, ep, cap, value, err):
    before = len(home.nodes["esp32-000000000001"].cmds_seen) + len(home.nodes["esp32-000000000002"].cmds_seen)
    r = set_(home, ep, cap, value)
    assert r["ok"] is False and r["error"] == err
    after = len(home.nodes["esp32-000000000001"].cmds_seen) + len(home.nodes["esp32-000000000002"].cmds_seen)
    assert after == before, "nothing may reach the device when validation fails"


def test_unknown_endpoint_and_malformed_ids(home):
    for bad in ("ep_deadbeef", "ep_XYZ", "", None, 7, "../../etc"):
        assert home.hub.executor.set_state(bad, "power", True)["error"] == "unknown_endpoint"


def test_nan_and_inf_rejected(home):
    assert set_(home, "light", "brightness", float("nan"))["error"] == "bad_type"
    assert set_(home, "light", "brightness", float("inf"))["error"] == "bad_type"


def test_disabled_endpoint(home):
    home.hub.admin.patch_endpoint(home.ids["light"], {"enabled": False}, "t")
    assert set_(home, "light", "power", True)["error"] == "disabled"
    assert home.ids["light"] not in [d["endpoint_id"] for d in home.hub.executor.list_devices()["devices"]]


def test_offline_endpoint_is_refused_not_queued(home):
    node = home.nodes["esp32-000000000001"]
    node.t.drop()
    home.broker.flush()
    r = set_(home, "light", "power", True)
    assert r["error"] == "offline"
    node.t.start()                      # comes back: the old command must NOT have been queued for it
    home.broker.flush()
    assert node.cmds_seen == []


def test_ack_timeout(home):
    home.nodes["esp32-000000000001"].drop_acks = True
    r = set_(home, "light", "power", True)
    assert r["ok"] is False and r["error"] == "ack_timeout"
    assert home.hub.bridge.pending_count() == 0


def test_late_ack_after_timeout_is_ignored(home):
    home.nodes["esp32-000000000001"].ack_delay = 0.9      # > 0.4 s timeout
    r = set_(home, "light", "power", True)
    assert r["error"] == "ack_timeout"
    import time
    time.sleep(1.2)
    home.broker.flush()
    assert home.hub.bridge.stats["ack_unmatched"] == 1


def test_duplicate_ack_first_wins(home):
    home.nodes["esp32-000000000001"].duplicate_acks = True
    assert set_(home, "light", "power", True)["ok"] is True
    home.broker.flush()
    assert home.hub.bridge.stats["ack_unmatched"] + home.hub.bridge.stats["ack_duplicate"] >= 1


def test_node_side_cmd_dedup_applies_once(home):
    node = home.nodes["esp32-000000000001"]
    cmd = {"cmd_id": "3f0c6a1e-0000-4000-8000-000000000001", "endpoint": "l1", "set": {"power": True}, "by": "agent"}
    topic = "home/v1/nodes/esp32-000000000001/cmd"
    home.hub.transport.publish(topic, json.dumps(cmd), 1, False)
    home.hub.transport.publish(topic, json.dumps(cmd), 1, False)
    home.broker.flush()
    assert len(node.cmds_seen) == 1


def test_ack_values_are_not_trusted_state_is(home):
    """The ack only means accepted; a lying ack cannot fake verification, and the real state wins."""
    home.nodes["esp32-000000000001"].lie_about_value = True
    r = set_(home, "light", "power", True)
    assert r["ok"] is True and r["status"] == "done" and r["verified"] is True      # state really changed


def test_state_that_never_follows_is_ok_but_unverified(home):
    home.nodes["esp32-000000000001"].ignore_state = True
    home.hub.cfg.verify_grace_s = 0.2
    r = set_(home, "light", "power", True)
    assert r["ok"] is True and r["status"] == "done" and r["verified"] is False     # a warning, not an error


def test_stale_matching_state_does_not_count_as_verification(home):
    """State already equals the target before the command: only a state message AFTER the send verifies it."""
    node = home.nodes["esp32-000000000001"]
    node.ignore_state = True
    home.hub.cfg.verify_grace_s = 0.2
    node.values["l1"]["power"] = True
    node.publish_state()
    home.broker.flush()
    assert set_(home, "light", "power", True)["verified"] is False


def test_device_error_codes_map(home):
    # the fake node validates with its own announce; force a device-side reject by bypassing hub range via a lock enum
    node = home.nodes["esp32-000000000001"]
    node._caps["l1"]["brightness"]["max"] = 60         # the board is stricter than the announce said
    r = set_(home, "light", "brightness", 80)
    assert r["error"] == "out_of_range"


def test_float_targets_and_integral_floats(home):
    assert set_(home, "light", "brightness", 70.0)["ok"] is True
    assert home.nodes["esp32-000000000001"].values["l1"]["brightness"] == 70


# ---------------------------------------------------------------------------------------------- confirmation
def test_risk_confirm_without_token_sends_nothing(home):
    r = set_(home, "lock", "lock", "unlocked", user_id=U)
    assert r == {"ok": False, "endpoint_id": home.ids["lock"], "capability": "lock", "value": "unlocked",
                 "error": "confirmation_required"}
    assert home.nodes["esp32-000000000002"].cmds_seen == []


def test_risk_confirm_with_valid_token_runs_once(home):
    action = set_state_action(home.ids["lock"], "lock", "unlocked")
    token = home.token(U, action)
    r = set_(home, "lock", "lock", "unlocked", user_id=U, token=token)
    assert r["ok"] is True
    assert home.nodes["esp32-000000000002"].values["d1"]["lock"] == "unlocked"
    again = set_(home, "lock", "lock", "unlocked", user_id=U, token=token)      # replay
    assert again["error"] == "confirmation_required"
    assert len(home.nodes["esp32-000000000002"].cmds_seen) == 1


def test_token_for_other_action_is_burned_and_denied(home):
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "locked"))
    r = set_(home, "lock", "lock", "unlocked", user_id=U, token=token)
    assert r["error"] == "confirmation_required"
    r = set_(home, "lock", "lock", "locked", user_id=U, token=token)           # burned by the mismatch
    assert r["error"] == "confirmation_required"
    assert home.nodes["esp32-000000000002"].cmds_seen == []


def test_token_of_another_user_denied_and_not_burned(home):
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"))
    assert set_(home, "lock", "lock", "unlocked", user_id="user:eve", token=token)["error"] == "confirmation_required"
    assert set_(home, "lock", "lock", "unlocked", user_id=U, token=token)["ok"] is True


def test_expired_token(home):
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"), ttl=60)
    home.clock.advance(61)
    assert set_(home, "lock", "lock", "unlocked", user_id=U, token=token)["error"] == "confirmation_required"


def test_invalid_action_does_not_burn_the_token(home):
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"))
    assert set_(home, "lock", "lock", "ajar", user_id=U, token=token)["error"] == "out_of_range"
    assert set_(home, "lock", "lock", "unlocked", user_id=U, token=token)["ok"] is True


def test_token_without_user_is_ignored(home):
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"))
    assert set_(home, "lock", "lock", "unlocked", token=token)["error"] == "confirmation_required"
    assert home.gw.calls == []


def test_gateway_down_fails_closed(home):
    import httpx
    from device_hub.gateway_client import GatewayClient

    def boom(request):
        raise httpx.ConnectError("down")

    home.hub.executor.gateway = GatewayClient("http://x", "s" * 40, 1.0, transport=httpx.MockTransport(boom))
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"))
    assert set_(home, "lock", "lock", "unlocked", user_id=U, token=token)["error"] == "confirmation_required"
    assert home.nodes["esp32-000000000002"].cmds_seen == []


def test_non_risky_endpoint_ignores_token(home):
    assert set_(home, "light", "power", True, user_id=U, token="x" * 43)["ok"] is True
    assert home.gw.calls == []


def test_canonical_hash_vectors_from_contract():
    for v in load_json(CONTRACTS / "samples" / "confirmation" / "hash-vectors.json")["vectors"]:
        action = v["input"]
        if action["type"] == "run_scene":
            action = run_scene_action(action["scene_id"], action["actions"])
        assert canonical_json(action) == v["canonical"], v["name"]
        assert action_hash(action) == v["sha256"], v["name"]


# ---------------------------------------------------------------------------------------------- reads
def test_read_sensor_value_unit_and_staleness(home):
    node = home.nodes["esp32-000000000001"]
    import time
    node.set_sensor("t1", "temperature", 23.5)
    home.broker.flush()
    home.clock.t = time.time()
    r = home.hub.executor.read_sensor(home.ids["temp"], "temperature")
    assert r["value"] == 23.5 and r["unit"] == "C" and r["stale"] is False
    home.clock.advance(31 * 3)          # > 3x interval_s (30)
    assert home.hub.executor.read_sensor(home.ids["temp"], "temperature")["stale"] is True
    home.clock.t = time.time()
    node.t.drop()
    home.broker.flush()
    assert home.hub.executor.read_sensor(home.ids["temp"], "temperature")["stale"] is True    # offline -> stale


def test_read_sensor_errors(home):
    assert home.hub.executor.read_sensor(home.ids["light"], "power")["error"] == "not_sensor"
    assert home.hub.executor.read_sensor(home.ids["temp"], "humidity")["error"] == "unknown_capability"
    assert home.hub.executor.read_sensor("ep_00000000", "temperature")["error"] == "unknown_endpoint"


def test_get_state_by_room_and_args(home):
    ex = home.hub.executor
    st = ex.get_state(room="bedroom")["states"]
    assert {s["endpoint_id"] for s in st} == {home.ids["light"], home.ids["temp"], home.ids["fan"]}
    assert ex.get_state(room="Garage")["error"] == "unknown_room"
    assert ex.get_state()["error"] == "bad_request"
    assert ex.get_state(endpoint_id=home.ids["light"], room="Hall")["error"] == "bad_request"


def test_offline_endpoint_returns_last_values_with_online_false(home):
    set_(home, "light", "brightness", 33)
    home.nodes["esp32-000000000001"].t.drop()
    home.broker.flush()
    s = home.hub.executor.get_state(endpoint_id=home.ids["light"])["states"][0]
    assert s["online"] is False and s["values"]["brightness"] == 33


def test_list_devices_shape_and_filters(home):
    out = home.hub.executor.list_devices()["devices"]
    assert len(out) == 4
    d = next(x for x in out if x["endpoint_id"] == home.ids["lock"])
    assert d["risk"] == "confirm" and d["room"] == "Hall" and d["capabilities"][0]["values"] == ["locked", "unlocked"]
    assert set(d) == {"endpoint_id", "name", "room", "tags", "type", "online", "risk", "capabilities"} and d["tags"] == []
    assert [x["name"] for x in home.hub.executor.list_devices(room="hall")["devices"]] == ["Front door"]
    assert [x["type"] for x in home.hub.executor.list_devices(etype="fan")["devices"]] == ["fan"]


def test_broker_link_down_means_nothing_is_online_and_nothing_is_queued(home):
    assert home.hub.registry.get_endpoint(home.ids["light"])["online"] is True
    home.hub.transport.stop()
    assert home.hub.registry.get_endpoint(home.ids["light"])["online"] is False
    assert home.hub.registry.get_node("esp32-000000000001")["online"] is False
    assert set_(home, "light", "power", True)["error"] == "offline"
    assert home.hub.executor.get_state(endpoint_id=home.ids["light"])["states"][0]["online"] is False
    assert home.nodes["esp32-000000000001"].cmds_seen == []
