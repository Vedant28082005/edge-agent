import inspect
import json
import uuid

import pytest

from conftest import NID

BASE = f"home/v1/nodes/{NID}/"


def test_boot_publishes_status_announce_state_retained(rig, validator):
    pubs = [(t, q, r) for _n, t, _p, q, r in rig.bus.published]
    assert pubs[0] == (BASE + "status", 1, True)
    assert rig.bus.raw("status")[0] == b"online"
    assert (BASE + "announce", 1, True) in pubs
    assert (BASE + "state", 1, True) in pubs
    assert rig.tr.subscriptions == [BASE + "cmd"]
    validator.validate("announce", rig.bus.on_topic("announce")[0])
    st = rig.last_state()
    assert st["endpoints"]["ch1"] == {"power": False, "speed": 2}        # boot_value applied
    assert st["endpoints"]["t1"]["temperature"] == 20.0                  # sensors sampled at boot
    assert set(st["endpoints"]) == {"ch1", "l1", "c1", "t1"}             # every endpoint, every capability


def test_lwt_is_registered_on_the_real_transport():
    from simesp32.transport import PahoTransport
    src = inspect.getsource(PahoTransport.__init__)
    assert 'will_set(will_topic, payload="offline", qos=1, retain=True)' in src


def test_set_applies_publishes_state_then_acks(rig):
    n = len(rig.bus.published)
    cid = rig.cmd("ch1", {"power": True, "speed": 4})
    new = rig.bus.published[n:]
    assert [t.rsplit("/", 1)[1] for _n, t, *_ in new] == ["state", "ack"]       # apply, publish state, ack
    assert rig.acks()[-1] == {"cmd_id": cid, "ok": True, "endpoint": "ch1",
                              "values": {"power": True, "speed": 4}, "error": None}
    assert rig.last_state()["endpoints"]["ch1"] == {"power": True, "speed": 4}


def test_desired_state_is_idempotent_not_a_toggle(rig):
    rig.cmd("ch1", {"power": True})
    rig.cmd("ch1", {"power": True})                       # new cmd_id, same desired state
    assert rig.last_state()["endpoints"]["ch1"]["power"] is True
    assert all(a["ok"] for a in rig.acks())


def test_dedup_replays_ack_and_does_not_reapply(rig):
    cid = rig.cmd("l1", {"brightness": 80})
    states = len(rig.bus.on_topic("state"))
    rig.node.values["l1"]["brightness"] = 10             # prove the retry is not re-applied
    rig.raw_cmd({"cmd_id": cid, "endpoint": "l1", "set": {"brightness": 80}, "by": "agent"})
    assert rig.node.values["l1"]["brightness"] == 10
    assert len(rig.bus.on_topic("state")) == states       # no state republish for a duplicate
    acks = [a for a in rig.acks() if a["cmd_id"] == cid]
    assert len(acks) == 2 and acks[0] == acks[1]          # same ack replayed
    assert rig.node.stats["cmds_applied"] == 1 and rig.node.stats["cmds_deduped"] == 1


def test_dedup_remembers_failures_too(rig):
    cid = rig.cmd("l1", {"brightness": 500})
    rig.raw_cmd({"cmd_id": cid, "endpoint": "l1", "set": {"brightness": 50}, "by": "agent"})   # same id, other body
    acks = [a for a in rig.acks() if a["cmd_id"] == cid]
    assert [a["error"] for a in acks] == ["out_of_range", "out_of_range"]
    assert rig.node.values["l1"]["brightness"] == 30


def test_dedup_cache_is_bounded(rig):
    first = rig.cmd("l1", {"brightness": 1})
    for i in range(70):
        rig.cmd("l1", {"brightness": i % 100})
    assert first not in rig.node.dedup and len(rig.node.dedup) == 64


@pytest.mark.parametrize("endpoint,sets,err", [
    ("nope", {"power": True}, "unknown_endpoint"),
    ("ch1", {"warp": True}, "unknown_capability"),
    ("ch1", {"brightness": 5}, "unknown_capability"),       # in the vocabulary, not on this endpoint
    ("t1", {"temperature": 5.0}, "unsupported"),            # sensors are read-only
    ("ch1", {"speed": 6}, "out_of_range"),
    ("ch1", {"speed": 0}, "out_of_range"),
    ("l1", {"brightness": 101}, "out_of_range"),
    ("l1", {"brightness": -1}, "out_of_range"),
    ("c1", {"position": 101}, "out_of_range"),
    ("l1", {"mode": "turbo"}, "out_of_range"),
    ("ch1", {"power": 1}, "bad_type"),
    ("ch1", {"speed": True}, "bad_type"),
    ("ch1", {"speed": 2.5}, "bad_type"),
    ("ch1", {"speed": "3"}, "bad_type"),
    ("l1", {"mode": 3}, "bad_type"),
])
def test_validation_errors(rig, endpoint, sets, err):
    before = json.dumps(rig.node.build_state()["endpoints"], sort_keys=True)
    n_states = len(rig.bus.on_topic("state"))
    rig.cmd(endpoint, sets)
    ack = rig.acks()[-1]
    assert ack["ok"] is False and ack["error"] == err and ack["values"] == {}
    assert json.dumps(rig.node.build_state()["endpoints"], sort_keys=True) == before
    assert len(rig.bus.on_topic("state")) == n_states     # nothing applied, no state publish
    assert rig.node.stats["violations"] == 0              # and the ack itself is contract-valid


def test_set_is_atomic(rig):
    rig.cmd("ch1", {"power": True, "speed": 99})
    assert rig.acks()[-1]["error"] == "out_of_range"
    assert rig.node.values["ch1"]["power"] is False


@pytest.mark.parametrize("body", [
    {"endpoint": "ch1", "set": {}, "by": "agent"},
    {"endpoint": "ch1", "by": "agent"},
    {"set": {"power": True}, "by": "agent"},
    {"endpoint": "ch1", "set": {"power": True}},
    {"endpoint": "ch1", "set": [1], "by": "agent"},
    {"cmd": "reboot"},
    {"cmd": "identify", "duration_s": 0},
    {"cmd": "identify"},
])
def test_malformed_cmd_with_valid_id_gets_an_error_ack(rig, body):
    cid = str(uuid.uuid4())
    rig.raw_cmd({"cmd_id": cid, **body})
    ack = rig.acks()[-1]
    assert ack["cmd_id"] == cid and ack["ok"] is False and ack["error"] in ("bad_request", "unsupported")
    assert rig.node.stats["violations"] == 0


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"endpoint":"ch1"}', b'{"cmd_id":"nope"}', b"", b"\xff\xfe"])
def test_garbage_without_cmd_id_is_dropped_silently(rig, payload):
    n = len(rig.bus.published)
    rig.raw_cmd(payload)
    assert len(rig.bus.published) == n


def test_cmd_on_foreign_topic_ignored(rig):
    n = len(rig.bus.published)
    body = json.dumps({"cmd_id": str(uuid.uuid4()), "cmd": "unprovision"}).encode()
    rig.tr.deliver("home/v1/nodes/esp32-bbbbbbbbbbbb/cmd", body)
    assert len(rig.bus.published) == n and not rig.node.stopped


def test_identify_acks_and_blinks_for_duration(rig):
    cid = str(uuid.uuid4())
    rig.raw_cmd({"cmd_id": cid, "cmd": "identify", "duration_s": 10})
    assert rig.acks()[-1] == {"cmd_id": cid, "ok": True, "endpoint": None, "values": {}, "error": None}
    assert rig.node.identify_until is not None
    rig.run(10.5)
    assert rig.node.identify_until is None


def test_unprovision_acks_goes_offline_and_stays_down(rig):
    cid = str(uuid.uuid4())
    rig.raw_cmd({"cmd_id": cid, "cmd": "unprovision"})
    assert rig.acks()[-1]["ok"] is True
    rig.run(1)
    assert rig.node.stopped and not rig.node.provisioned
    assert rig.bus.raw("status")[-1] == b"offline"
    assert rig.tr.connected is False
    n = len(rig.bus.published)
    rig.run(200)                                          # no reconnect, no heartbeat
    rig.raw_cmd({"cmd_id": str(uuid.uuid4()), "endpoint": "ch1", "set": {"power": True}, "by": "agent"})
    assert len(rig.bus.published) == n


def test_state_on_sensor_interval_and_heartbeat(rig):
    n0 = len(rig.bus.on_topic("state"))
    rig.run(5.2)                                          # sensors have interval_s=5
    states = rig.bus.on_topic("state")
    assert len(states) == n0 + 1
    assert states[-1]["endpoints"]["t1"]["temperature"] == 21.5     # sequence generator advanced
    n1 = len(states)
    rig.run(60)
    assert len(rig.bus.on_topic("state")) > n1


def test_heartbeat_without_sensors(make_rig):
    r = make_rig("""
node_id: esp32-a1b2c3d4e599
heartbeat_s: 30
endpoints:
  - {local_id: l1, type: light, capabilities: [{id: power, boot_value: false}]}
""")
    n = len(r.bus.on_topic("state"))
    r.run(29)
    assert len(r.bus.on_topic("state")) == n
    r.run(2)
    assert len(r.bus.on_topic("state")) == n + 1


def test_reconnect_republishes_online_and_state_but_not_announce(rig):
    rig.tr.drop(2)
    assert rig.bus.raw("status")[-1] == b"offline"        # LWT fired by the broker
    n_state = len(rig.bus.on_topic("state"))
    rig.run(2.5)
    assert rig.bus.raw("status")[-1] == b"online"
    assert len(rig.bus.on_topic("state")) > n_state
    assert len(rig.bus.on_topic("announce")) == 1


def test_every_published_message_was_schema_validated(rig):
    rig.cmd("ch1", {"power": True})
    rig.cmd("zzz", {"power": True})
    rig.run(70)
    assert rig.node.stats["violations"] == 0


def test_invalid_outgoing_message_is_blocked_before_publish(rig):
    rig.node.values["ch1"]["speed"] = "x" * 40            # state schema: strings max 24
    n = len(rig.bus.on_topic("state"))
    rig.cmd("l1", {"brightness": 5})                      # triggers a state publish
    assert len(rig.bus.on_topic("state")) == n
    assert rig.node.stats["violations"] >= 1
    assert "state violates contract" in rig.node.violation_log[0]


def test_ts_is_unix_seconds(rig):
    assert rig.last_state()["ts"] > 1_700_000_000
