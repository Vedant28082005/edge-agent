import itertools
import json

import jsonschema
import pytest

from agent.devices import DeviceRegistry
from agent.mock import demo_devices
from agent.schema import BRIGHTNESS_FLOOR, build_plan_schema, repair_plan_calls, schema_endpoint_ids, strip_forbidden, validate_call


def reg_of(devs=None):
    r = DeviceRegistry()
    r.replace(devs or demo_devices(), 1)
    return r


def plan(*calls, say="ok"):
    return {"calls": [{"tool": t, "args": a} for t, a in calls], "say": say}


def valid(schema, doc):
    try:
        jsonschema.validate(doc, schema)
        return True
    except jsonschema.ValidationError:
        return False


def test_valid_plans_pass():
    s = build_plan_schema(reg_of())
    assert valid(s, plan(("set_state", {"endpoint_id": "ep_a1000002", "capability": "power", "value": True})))
    assert valid(s, plan(("set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 5})))
    assert valid(s, plan(("set_state", {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"})))
    assert valid(s, plan(("read_sensor", {"endpoint_id": "ep_a1000003", "capability": "temperature"})))
    assert valid(s, plan(("get_state", {"room": "Bedroom"})))
    assert valid(s, plan(("list_devices", {})))
    assert valid(s, plan(("run_scene", {"scene": "Good night"})))
    assert valid(s, plan())


@pytest.mark.parametrize("call", [
    ("set_state", {"endpoint_id": "ep_dead0000", "capability": "power", "value": True}),       # no such endpoint
    ("set_state", {"endpoint_id": "ep_a1000002", "capability": "lock", "value": "locked"}),    # capability the fan lacks
    ("set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 9}),          # out of range
    ("set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 0}),
    ("set_state", {"endpoint_id": "ep_a1000002", "capability": "power", "value": "yes"}),      # wrong type
    ("set_state", {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "open"}),      # bad enum
    ("set_state", {"endpoint_id": "ep_a1000003", "capability": "temperature", "value": 20}),   # sensor is not an actuator
    ("set_state", {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 101}),
    ("read_sensor", {"endpoint_id": "ep_a1000002", "capability": "temperature"}),              # fan has no sensor
    ("get_state", {"endpoint_id": "ep_00000000"}),
    ("get_state", {"room": "Moon base"}),
    ("set_state", {"endpoint_id": "ep_a1000002", "capability": "power", "value": True, "confirmation_token": "x"}),
    ("delete_device", {"endpoint_id": "ep_a1000002"}),                                          # no sixth tool
    ("set_state", {"endpoint_id": "ep_a1000002", "capability": "power"}),                       # missing value
])
def test_invalid_calls_cannot_be_expressed(call):
    s = build_plan_schema(reg_of())
    assert not valid(s, plan(call))


def test_at_most_four_calls_and_say_length():
    s = build_plan_schema(reg_of())
    c = ("get_state", {"room": "Bedroom"})
    assert valid(s, plan(c, c, c, c)) and not valid(s, plan(c, c, c, c, c))
    assert not valid(s, plan(say="x" * 500))


def test_every_endpoint_in_schema_is_in_the_registry_and_follows_it():
    r = reg_of()
    s = build_plan_schema(r)
    assert schema_endpoint_ids(s) == set(r.devices)
    r2 = reg_of(demo_devices()[:2])                          # devices removed -> impossible to name them
    s2 = build_plan_schema(r2)
    assert schema_endpoint_ids(s2) == {"ep_a1000001", "ep_a1000002"}
    assert not valid(s2, plan(("set_state", {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "locked"})))


def test_ranges_follow_the_announce():
    devs = demo_devices()
    devs[1]["capabilities"][1] = {"id": "speed", "kind": "actuator", "type": "int", "min": 1, "max": 3}
    s = build_plan_schema(reg_of(devs))

    def ok(v):
        return valid(s, plan(("set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": v})))

    assert ok(3) and not ok(4)


def test_capability_without_known_range_is_not_offered():
    devs = demo_devices()
    devs[1]["capabilities"] = [{"id": "speed", "kind": "actuator", "type": "int"}]      # speed needs min/max
    r = reg_of(devs)
    s = build_plan_schema(r)
    assert not valid(s, plan(("set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 2})))
    assert validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 2})[0] == "out_of_range"


def test_empty_registry_allows_no_device_calls():
    s = build_plan_schema(DeviceRegistry())
    assert valid(s, plan())
    assert not valid(s, plan(("get_state", {"room": "Bedroom"})))
    assert not valid(s, plan(("set_state", {"endpoint_id": "ep_a1000002", "capability": "power", "value": True})))


def test_scene_names_enum_when_configured():
    s = build_plan_schema(reg_of(), ("Good night", "Movie"))
    assert valid(s, plan(("run_scene", {"scene": "Movie"}))) and not valid(s, plan(("run_scene", {"scene": "Nope"})))


def test_schema_and_validator_agree_on_a_grid():
    r = reg_of()
    s = build_plan_schema(r)
    eps = list(r.devices) + ["ep_dead0000"]
    caps = ["power", "speed", "lock", "brightness", "position", "temperature"]
    vals = [True, False, 0, 1, 5, 50, 100, 101, "locked", "unlocked", "x"]
    for ep, cap, v in itertools.product(eps, caps, vals):
        a = {"endpoint_id": ep, "capability": cap, "value": v}
        in_schema = valid(s, plan(("set_state", a)))
        err, _ = validate_call(r, "set_state", a)
        assert in_schema == (err is None), (a, err)


def test_validate_call_rejections_and_strip():
    r = reg_of()
    assert validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "power", "value": True})[0] is None
    assert validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": True})[0] == "bad_type"
    assert validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 3.5})[0] == "bad_type"
    err, a = validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 3.0})
    assert err is None and a["value"] == 3 and isinstance(a["value"], int)
    assert validate_call(r, "reboot_hub", {})[0] == "unknown_tool"
    err, a = validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "power", "value": True,
                                              "confirmation_token": "T", "user_id": "bob", "call_id": "c"})
    assert err is None and set(a) == {"endpoint_id", "capability", "value"}
    clean, removed = strip_forbidden({"a": 1, "confirmation_token": "T", "_meta": {}})
    assert clean == {"a": 1} and sorted(removed) == ["_meta", "confirmation_token"]
    json.dumps(build_plan_schema(r))                                 # serialisable for llama-server


# ------------------------------------------------------------------------------------------------ S8e brightness guard
def test_repair_lifts_brightness_zero_paired_with_power_on():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}},
             {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    out = repair_plan_calls(calls, "turn on the ceiling light", reg_of())
    assert out[1]["args"]["value"] == BRIGHTNESS_FLOOR
    assert out[0] == calls[0]                              # the power call is untouched


def test_repair_lifts_a_lone_brightness_zero_call_on_a_turn_on_request():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    out = repair_plan_calls(calls, "turn on the ceiling light", reg_of())
    bright = next(c for c in out if c["args"]["capability"] == "brightness")
    assert bright["args"]["value"] == BRIGHTNESS_FLOOR


def test_repair_leaves_an_explicit_level_alone():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}},
             {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    for text in ("turn on the ceiling light at 0", "set the ceiling light to zero brightness", "set it to 0%"):
        out = repair_plan_calls(calls, text, reg_of())
        assert out[1]["args"]["value"] == 0, text


def test_repair_leaves_a_genuine_power_off_alone():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": False}},
             {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    out = repair_plan_calls(calls, "turn off the ceiling light", reg_of())
    assert out[1]["args"]["value"] == 0


def test_repair_only_touches_brightness_zero_for_the_matching_endpoint():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000006", "capability": "power", "value": False}},
             {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    out = repair_plan_calls(calls, "turn off the lamp and turn on the ceiling light", reg_of())
    bright = next(c for c in out if c["args"]["capability"] == "brightness")
    assert bright["args"]["value"] == BRIGHTNESS_FLOOR


# ------------------------------------------------------------------------------------------------ S8f brightness-implies-on
def test_repair_adds_a_missing_power_on_before_a_positive_brightness():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 40}}]
    out = repair_plan_calls(calls, "set the ceiling light to 40%", reg_of())
    assert [c["args"]["capability"] for c in out] == ["power", "brightness"]
    assert out[0]["args"] == {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}
    assert out[1]["args"]["value"] == 40


def test_repair_does_not_duplicate_an_explicit_power_call():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": False}},
             {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 40}}]
    out = repair_plan_calls(calls, "set the ceiling light to 40%", reg_of())
    assert out == calls                                    # an explicit power call (even False) is never overridden


def test_repair_never_adds_power_on_for_brightness_zero():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    out = repair_plan_calls(calls, "set the ceiling light to 0%", reg_of())
    assert out == calls                                    # explicit zero: no floor lift, no power-on either


def test_repair_lifts_the_floor_then_adds_power_on_for_a_lone_brightness_zero():
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}]
    out = repair_plan_calls(calls, "turn on the lone ceiling light", reg_of())
    assert [c["args"]["capability"] for c in out] == ["power", "brightness"]
    assert out[0]["args"]["value"] is True
    assert out[1]["args"]["value"] == BRIGHTNESS_FLOOR
