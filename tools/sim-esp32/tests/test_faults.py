"""Fault injection. The sim must stay contract-valid everywhere except the deliberate malformed-announce mode."""
import json
import uuid

import pytest

from simesp32.cli import apply_cli_faults
from simesp32.config import ConfigError, Faults, _node
from simesp32.schemas import SchemaViolation

LIGHT = """
node_id: esp32-a1b2c3d4e5f1
faults: %s
endpoints:
  - {local_id: l1, type: light, capabilities: [{id: power, boot_value: false}, {id: brightness, boot_value: 10}]}
"""


def light(make_rig, faults):
    return make_rig(LIGHT % json.dumps(faults))


def test_drop_first_acks_hub_retry_gets_replayed_ack(make_rig):
    r = light(make_rig, {"drop_first_acks": 1})
    cid = r.cmd("l1", {"power": True})
    assert r.acks() == [] and r.node.values["l1"]["power"] is True       # applied, ack lost
    r.raw_cmd({"cmd_id": cid, "endpoint": "l1", "set": {"power": True}, "by": "user:test"})   # hub retries
    assert [a["cmd_id"] for a in r.acks()] == [cid] and r.acks()[0]["ok"]
    assert r.node.stats["cmds_applied"] == 1 and r.node.stats["acks_dropped"] == 1


def test_drop_ack_probability_is_seeded_and_roughly_right(make_rig):
    r = light(make_rig, {"drop_ack": 0.5})
    for i in range(200):
        r.cmd("l1", {"brightness": i % 100})
    sent = len(r.acks())
    assert 70 < sent < 130 and sent + r.node.stats["acks_dropped"] == 200
    r2 = light(make_rig, {"drop_ack": 0.5})
    for i in range(200):
        r2.cmd("l1", {"brightness": i % 100})
    assert len(r2.acks()) == sent                                          # same seed, same outcome


def test_drop_ack_zero_and_one(make_rig):
    r = light(make_rig, {"drop_ack": 1.0})
    r.cmd("l1", {"power": True})
    assert r.acks() == []
    r = light(make_rig, {"drop_ack": 0.0})
    r.cmd("l1", {"power": True})
    assert len(r.acks()) == 1


def test_delayed_ack_arrives_after_hub_timeout_but_state_is_immediate(make_rig):
    r = light(make_rig, {"delay_ack_s": 4})
    cid = r.cmd("l1", {"power": True})
    assert r.acks() == []
    assert r.last_state()["endpoints"]["l1"]["power"] is True
    r.run(3.0)
    assert r.acks() == []                                                  # hub's 3 s timeout has passed
    r.run(1.2)
    assert [a["cmd_id"] for a in r.acks()] == [cid]


def test_duplicate_delivery_applies_once_and_acks_each_copy(make_rig):
    r = light(make_rig, {"duplicate_cmd": 2})
    n_states = len(r.bus.on_topic("state"))
    cid = r.cmd("l1", {"power": True})
    assert r.node.stats["cmds_applied"] == 1 and r.node.stats["cmds_deduped"] == 2
    assert len(r.bus.on_topic("state")) == n_states + 1
    acks = [a for a in r.acks() if a["cmd_id"] == cid]
    assert len(acks) == 3 and all(a == acks[0] for a in acks)


def test_duplicate_ack_publishes_twice(make_rig):
    r = light(make_rig, {"duplicate_ack": True})
    r.cmd("l1", {"power": True})
    assert len(r.acks()) == 2 and r.acks()[0] == r.acks()[1]


def test_flapping_drops_unclean_then_reconnects_repeatedly(make_rig):
    r = light(make_rig, {"flapping": {"up_s": 10, "down_s": 3}})
    r.run(10.2)
    assert r.tr.connected is False and r.bus.raw("status")[-1] == b"offline"   # LWT
    r.run(3.1)
    assert r.tr.connected is True and r.bus.raw("status")[-1] == b"online"
    r.run(14)
    assert r.bus.raw("status").count(b"offline") >= 2
    anns = r.bus.raw("announce")
    assert len(anns) >= 2 and len(set(anns)) == 1                               # re-announced identically on every connect
    assert r.node.stats["violations"] == 0


def test_flapping_longer_than_failsafe_triggers_failsafe(make_rig):
    r = make_rig("""
node_id: esp32-a1b2c3d4e5f2
failsafe_after_s: 5
faults: {flapping: {up_s: 10, down_s: 8}}
endpoints:
  - {local_id: l1, type: light, capabilities: [{id: power, boot_value: false, failsafe_value: false}]}
""")
    r.cmd("l1", {"power": True})
    r.run(16.5)
    assert r.node.values["l1"]["power"] is False


@pytest.mark.parametrize("mode", ["invalid_json", "missing_field", "wrong_schema", "oversize", "unknown_capability"])
def test_malformed_announce_modes_violate_the_schema(make_rig, validator, mode):
    r = light(make_rig, {"malformed_announce": mode})
    raw = r.bus.raw("announce")[0]
    if mode == "invalid_json":
        with pytest.raises(ValueError):
            json.loads(raw)
    else:
        with pytest.raises(SchemaViolation):
            validator.validate("announce", json.loads(raw))
    if mode == "oversize":
        assert len(raw) > 16 * 1024


def test_node_id_mismatch_announce_is_schema_valid_but_wrong_for_the_topic(make_rig, validator):
    r = light(make_rig, {"malformed_announce": "node_id_mismatch"})
    ann = json.loads(r.bus.raw("announce")[0])
    validator.validate("announce", ann)                                    # only the hub can catch this one
    assert ann["node_id"] != r.cfg.node_id


def test_unknown_capability_announce_contains_teleport_but_state_stays_clean(make_rig):
    r = light(make_rig, {"malformed_announce": "unknown_capability"})
    ann = json.loads(r.bus.raw("announce")[0])
    assert "teleport" in [c["id"] for c in ann["endpoints"][0]["capabilities"]]
    assert "teleport" not in json.dumps(r.last_state())


@pytest.mark.parametrize("mode", ["invalid_json", "unknown_capability", "wrong_schema"])
def test_everything_else_still_validated_in_malformed_mode(make_rig, mode):
    r = light(make_rig, {"malformed_announce": mode})
    r.cmd("l1", {"power": True})
    r.cmd("l1", {"brightness": 500})
    r.run(70)
    assert r.node.stats["violations"] == 0
    assert [a["ok"] for a in r.acks()] == [True, False]


def test_announce_without_fault_is_never_malformed(make_rig, validator):
    r = light(make_rig, {})
    validator.validate("announce", json.loads(r.bus.raw("announce")[0]))


# --- configuration of faults
def test_cli_fault_overrides():
    n = _node(json.loads('{"node_id":"esp32-a1b2c3d4e5f3","endpoints":[{"local_id":"l1","type":"light",'
                         '"capabilities":[{"id":"power","boot_value":false}]}]}'), 0)
    apply_cli_faults([n], ["drop_ack=0.25", "flapping=10:4", "esp32-a1b2c3d4e5f3:delay_ack_s=2.5",
                           "malformed_announce=oversize"])
    assert n.faults.drop_ack == 0.25 and n.faults.flapping == (10.0, 4.0)
    assert n.faults.delay_ack_s == 2.5 and n.faults.malformed_announce == "oversize"


@pytest.mark.parametrize("spec", ["bogus=1", "drop_ack=2", "malformed_announce=nope", "flapping=1", "esp32-ffffffffffff:drop_ack=1"])
def test_bad_fault_specs_are_rejected(spec):
    n = _node(json.loads('{"node_id":"esp32-a1b2c3d4e5f3","endpoints":[{"local_id":"l1","type":"light",'
                         '"capabilities":[{"id":"power","boot_value":false}]}]}'), 0)
    with pytest.raises(ConfigError):
        apply_cli_faults([n], [spec])
