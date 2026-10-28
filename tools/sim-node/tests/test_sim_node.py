import json
import re
import uuid

import pytest

import sim_node
from sim_node import SimNode, build_config, parse_latency, resolve_node_id
from profiles import PROFILES
from simesp32.clock import ManualClock
from simesp32.config import ConfigError
from simesp32.schemas import Validator
from simesp32.transport import MemoryBus, MemoryTransport

NID = "esp32-0123456789ab"


@pytest.fixture(scope="module")
def validator():
    return Validator()


class Rig:
    def __init__(self, validator, profile, latency=(0.0, 0.0), fail_rate=0.0, seed=1):
        self.cfg = build_config(profile, NID, seed=seed)
        self.clock = ManualClock()
        self.bus = MemoryBus()
        self.tr = MemoryTransport(NID, self.bus, self.clock, f"home/v1/nodes/{NID}/status")
        self.node = SimNode(self.cfg, self.tr, validator, self.clock, latency_ms=latency, fail_rate=fail_rate,
                            rng_seed=seed)
        self.node.boot()
        self.tr.start()

    def cmd(self, endpoint, set_, cmd_id=None):
        cid = cmd_id or str(uuid.uuid4())
        self.bus.inject(NID, {"cmd_id": cid, "endpoint": endpoint, "set": set_, "by": "user:test"})
        return cid

    def run(self, seconds, step=0.01):
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end - 1e-9:
            self.clock.advance(step)
            self.tr.poll()
            self.node.tick()

    def topics(self, suffix):
        return self.bus.on_topic(suffix)


# ------------------------------------------------------------------ ids and profiles
def test_alias_maps_to_contract_id_deterministically():
    a = resolve_node_id("sim-fan-01")
    assert re.fullmatch(r"esp32-[0-9a-f]{12}", a)
    assert a == resolve_node_id("sim-fan-01") != resolve_node_id("sim-fan-02")
    assert resolve_node_id(NID) == NID


def test_alias_rule_matches_documented_sha256():
    import hashlib
    assert resolve_node_id("sim-fan-01") == "esp32-" + hashlib.sha256(b"sim-fan-01").hexdigest()[:12]


@pytest.mark.parametrize("bad", ["", "has space", "x" * 49, "ünï"])
def test_bad_alias_rejected(bad):
    with pytest.raises(ConfigError):
        resolve_node_id(bad)


def test_latency_parse():
    assert parse_latency("50:300") == (50.0, 300.0)
    assert parse_latency("0") == (0.0, 0.0)
    with pytest.raises(ConfigError):
        parse_latency("300:50")


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_profile_announces_validly_and_is_marked_sim(validator, profile):
    cfg = build_config(profile, NID)
    ann = cfg.announce()
    validator.validate("announce", ann)
    assert ann["hw"].startswith("sim-") and ann["fw"].startswith("sim-")


def test_profile_endpoint_ids_match_firmware_examples():
    shape = {p: {e["local_id"]: [c["id"] for c in e["capabilities"]] for e in v["endpoints"]}
             for p, v in PROFILES.items()}
    assert shape["light"] == {"l1": ["power", "brightness"]}
    assert shape["fan"] == {"ch1": ["power", "speed"]}
    assert shape["curtain"] == {"c1": ["position"]}
    assert shape["dht"] == {"t1": ["temperature", "humidity"]}
    assert shape["relay4"] == {"r1": ["power"], "r2": ["power"], "r3": ["power"], "r4": ["power"]}
    assert shape["sensors"] == {"door": ["contact"], "hall": ["motion"], "lux": ["illuminance"]}
    ann = build_config("curtain", NID).announce()
    pos = ann["endpoints"][0]["capabilities"][0]
    assert pos["settle_s"] == 30 and ann["endpoints"][0]["type"] == "cover"


# ------------------------------------------------------------------ connect lifecycle
def test_connect_order_and_retained_flags(validator):
    r = Rig(validator, "fan")
    pub = [(t.rsplit("/", 1)[1], retain) for _, t, _, _, retain in r.bus.published]
    assert pub[:3] == [("status", True), ("announce", True), ("state", True)]
    assert r.tr.subscriptions == [f"home/v1/nodes/{NID}/cmd"]
    assert r.bus.raw("status")[0] == b"online"


def test_shutdown_publishes_offline(validator):
    r = Rig(validator, "fan")
    r.node.shutdown()
    assert r.bus.raw("status")[-1] == b"offline"


# ------------------------------------------------------------------ latency and command state machine
def test_command_applied_after_latency_then_state_then_ack(validator):
    r = Rig(validator, "fan", latency=(100.0, 100.0))
    r.cmd("ch1", {"power": True, "speed": 3})
    r.run(0.05)
    assert r.topics("ack") == []                      # not yet
    r.run(0.1)
    ack = r.topics("ack")[0]
    assert ack["ok"] and ack["values"] == {"power": True, "speed": 3} and ack["error"] is None
    kinds = [t.rsplit("/", 1)[1] for _, t, *_ in r.bus.published][-2:]
    assert kinds == ["state", "ack"]                   # apply -> state -> ack
    assert r.topics("state")[-1]["endpoints"]["ch1"] == {"power": True, "speed": 3}


def test_latency_stays_within_range_and_preserves_order(validator):
    r = Rig(validator, "relay4", latency=(50.0, 300.0), seed=7)
    ids = [r.cmd("r1", {"power": bool(i % 2)}) for i in range(8)]
    r.run(0.04)
    assert r.topics("ack") == []                       # nothing earlier than the minimum latency
    r.run(0.3 * 8 + 0.1)
    assert [a["cmd_id"] for a in r.topics("ack")] == ids


@pytest.mark.parametrize("ep,payload,err", [
    ("nope", {"power": True}, "unknown_endpoint"),
    ("ch1", {"teleport": 1}, "unknown_capability"),
    ("ch1", {"speed": 9}, "out_of_range"),
    ("ch1", {"speed": "fast"}, "bad_type"),
    ("ch1", {"power": 1}, "bad_type"),
])
def test_error_codes(validator, ep, payload, err):
    r = Rig(validator, "fan")
    r.cmd(ep, payload)
    r.run(0.05)
    ack = r.topics("ack")[0]
    assert not ack["ok"] and ack["error"] == err


def test_atomic_command(validator):
    r = Rig(validator, "fan")
    r.cmd("ch1", {"power": True, "speed": 99})
    r.run(0.05)
    assert not r.topics("ack")[0]["ok"]
    assert r.topics("state")[-1]["endpoints"]["ch1"]["power"] is False


def test_dedup_replays_ack_without_reapplying(validator):
    r = Rig(validator, "light")
    cid = r.cmd("l1", {"brightness": 80})
    r.run(0.05)
    states = len(r.topics("state"))
    r.cmd("l1", {"brightness": 10}, cmd_id=cid)        # same cmd_id, different body: must not apply
    r.run(0.05)
    acks = r.topics("ack")
    assert len(acks) == 2 and acks[0] == acks[1] and acks[1]["values"] == {"brightness": 80}
    assert len(r.topics("state")) == states
    assert r.node.stats["cmds_deduped"] == 1


def test_dedup_ring_is_16(validator):
    r = Rig(validator, "light")
    first = r.cmd("l1", {"brightness": 1})
    for i in range(16):
        r.cmd("l1", {"brightness": 2 + i})
    r.run(0.1)
    r.cmd("l1", {"brightness": 99}, cmd_id=first)      # evicted -> applied again, not replayed
    r.run(0.1)
    assert r.node.stats["cmds_deduped"] == 0
    assert r.topics("state")[-1]["endpoints"]["l1"]["brightness"] == 99


def test_identify_acks_with_null_endpoint(validator):
    r = Rig(validator, "light")
    cid = str(uuid.uuid4())
    r.bus.inject(NID, {"cmd_id": cid, "cmd": "identify", "duration_s": 5})
    r.run(0.05)
    ack = r.topics("ack")[0]
    assert ack == {"cmd_id": cid, "ok": True, "endpoint": None, "values": {}, "error": None}


# ------------------------------------------------------------------ fail-rate
def test_fail_rate_one_refuses_with_busy_and_does_not_apply(validator):
    r = Rig(validator, "fan", fail_rate=1.0)
    r.cmd("ch1", {"power": True})
    r.run(0.05)
    ack = r.topics("ack")[0]
    assert ack["ok"] is False and ack["error"] == "busy" and ack["endpoint"] == "ch1"
    assert r.topics("state")[-1]["endpoints"]["ch1"]["power"] is False
    assert r.node.stats["cmds_failed_injected"] == 1


def test_fail_rate_zero_never_fails_and_identify_exempt(validator):
    r = Rig(validator, "fan", fail_rate=0.0)
    for i in range(20):
        r.cmd("ch1", {"speed": 1 + i % 5})
    r.run(0.1)
    assert all(a["ok"] for a in r.topics("ack"))
    r2 = Rig(validator, "fan", fail_rate=1.0)
    r2.bus.inject(NID, {"cmd_id": str(uuid.uuid4()), "cmd": "identify", "duration_s": 1})
    r2.run(0.05)
    assert r2.topics("ack")[0]["ok"]


def test_fail_rate_is_roughly_proportional(validator):
    r = Rig(validator, "light", fail_rate=0.3, seed=5)
    n = 400
    for i in range(n):
        r.cmd("l1", {"brightness": i % 100})
        r.run(0.02)
    failed = sum(1 for a in r.topics("ack") if not a["ok"])
    assert 0.2 * n < failed < 0.4 * n


def test_failed_cmd_is_cached_for_dedup(validator):
    r = Rig(validator, "fan", fail_rate=1.0)
    cid = r.cmd("ch1", {"power": True})
    r.run(0.05)
    r.cmd("ch1", {"power": True}, cmd_id=cid)
    r.run(0.05)
    a = r.topics("ack")
    assert len(a) == 2 and a[0] == a[1] and r.node.stats["cmds_failed_injected"] == 1


# ------------------------------------------------------------------ profile behaviour
def test_curtain_ack_is_target_state_follows(validator):
    r = Rig(validator, "curtain")
    r.cmd("c1", {"position": 50})
    r.run(0.05)
    assert r.topics("ack")[0]["values"] == {"position": 50}
    assert r.topics("state")[-1]["endpoints"]["c1"]["position"] < 50
    r.run(15)
    assert r.topics("state")[-1]["endpoints"]["c1"]["position"] == 50


def test_dht_random_walk_is_slow_and_bounded(validator):
    r = Rig(validator, "dht")
    temps = []
    for _ in range(40):
        r.run(30)
        temps.append(r.topics("state")[-1]["endpoints"]["t1"]["temperature"])
    assert all(15.0 <= t <= 40.0 for t in temps)
    assert all(abs(b - a) <= 0.2 for a, b in zip(temps, temps[1:]))
    assert len(set(temps)) > 3


def test_relay4_boot_and_max_on(validator):
    r = Rig(validator, "relay4")
    eps = r.topics("state")[0]["endpoints"]
    assert {k: v["power"] for k, v in eps.items()} == {"r1": False, "r2": False, "r3": True, "r4": False}
    r.cmd("r1", {"power": True})
    r.run(3601, step=1.0)
    assert r.topics("state")[-1]["endpoints"]["r1"]["power"] is False


# ------------------------------------------------------------------ whole-program against the in-memory broker
def test_main_mock_check_runs_end_to_end(capsys):
    assert sim_node.main(["--profile", "light", "--node-id", "sim-light-01", "--check"]) == 0
    out = capsys.readouterr().out
    nid = resolve_node_id("sim-light-01")
    assert nid in out
    ann = json.loads(out[out.index("{"):])
    assert ann["hw"] == "sim-light" and ann["node_id"] == nid


def test_main_rejects_missing_credentials(tmp_path, capsys):
    rc = sim_node.main(["--profile", "fan", "--node-id", "sim-fan-01", "--sim-dir", str(tmp_path)])
    assert rc == 2 and "CA file not found" in capsys.readouterr().err
