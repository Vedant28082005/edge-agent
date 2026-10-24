import itertools
import json

import jsonschema
import pytest

from agent.aliases import AliasMap, build_aliases
from agent.devices import DeviceRegistry
from agent.mock import demo_devices
from agent.schema import BRIGHTNESS_FLOOR, build_plan_schema, repair_plan_calls, schema_endpoint_ids, strip_forbidden, validate_call

# demo_devices() ids sort to e1..e6 in the same order: Ceiling light, Fan, Temp sensor, Front door, Blinds, Lamp.
E1, E2, E3, E4, E5, E6 = "e1", "e2", "e3", "e4", "e5", "e6"


def reg_of(devs=None):
    r = DeviceRegistry()
    r.replace(devs or demo_devices(), 1)
    return r


def schema_of(devs=None, scene_names=None):
    r = reg_of(devs)
    a = build_aliases(r)
    return r, a, build_plan_schema(r, a, scene_names)


def valid(schema, doc):
    try:
        jsonschema.validate(doc, schema)
        return True
    except jsonschema.ValidationError:
        return False


def test_valid_plans_pass():
    r, a, s = schema_of()
    assert valid(s, [["s", E2, "power", True]])
    assert valid(s, [["s", E2, "speed", 5]])
    assert valid(s, [["s", E4, "lock", "unlocked"]])
    assert valid(s, [["r", E3, "temperature"]])
    assert valid(s, [["gr", "Bedroom"]])
    assert valid(s, [["l", None, None]])
    assert valid(s, [["n", "Good night"]])
    assert valid(s, [])


@pytest.mark.parametrize("call", [
    ["s", "e99", "power", True],               # no such alias
    ["s", E2, "lock", "locked"],                # capability the fan lacks
    ["s", E2, "speed", 9],                      # out of range
    ["s", E2, "speed", 0],
    ["s", E2, "power", "yes"],                  # wrong type
    ["s", E4, "lock", "open"],                  # bad enum
    ["s", E3, "temperature", 20],                # sensor is not an actuator
    ["s", E1, "brightness", 101],
    ["r", E2, "temperature"],                    # fan has no sensor
    ["g", "e99"],                                # no such alias
    ["gr", "Moon base"],
    ["s", E2, "power", True, "x"],              # extra tuple item
    ["z", E2],                                   # no sixth tool / unknown code
    ["s", E2, "power"],                          # missing value
])
def test_invalid_calls_cannot_be_expressed(call):
    _, _, s = schema_of()
    assert not valid(s, [call])


def test_at_most_four_calls():
    _, _, s = schema_of()
    c = ["gr", "Bedroom"]
    assert valid(s, [c, c, c, c]) and not valid(s, [c, c, c, c, c])


def test_every_endpoint_in_schema_is_in_the_registry_and_follows_it():
    r, a, s = schema_of()
    assert schema_endpoint_ids(s, a) == set(r.devices)
    r2, a2, s2 = schema_of(demo_devices()[:2])               # devices removed -> impossible to name them
    assert schema_endpoint_ids(s2, a2) == {"ep_a1000001", "ep_a1000002"}
    assert not valid(s2, [["s", a2.alias("ep_a1000004"), "lock", "locked"]])


def test_ranges_follow_the_announce():
    devs = demo_devices()
    devs[1]["capabilities"][1] = {"id": "speed", "kind": "actuator", "type": "int", "min": 1, "max": 3}
    _, _, s = schema_of(devs)

    def ok(v):
        return valid(s, [["s", E2, "speed", v]])

    assert ok(3) and not ok(4)


def test_capability_without_known_range_is_not_offered():
    devs = demo_devices()
    devs[1]["capabilities"] = [{"id": "speed", "kind": "actuator", "type": "int"}]      # speed needs min/max
    r, _, s = schema_of(devs)
    assert not valid(s, [["s", E2, "speed", 2]])
    assert validate_call(r, "set_state", {"endpoint_id": "ep_a1000002", "capability": "speed", "value": 2})[0] == "out_of_range"


def test_empty_registry_allows_no_device_calls():
    s = build_plan_schema(DeviceRegistry(), AliasMap())
    assert valid(s, [])
    assert not valid(s, [["gr", "Bedroom"]])
    assert not valid(s, [["s", "e1", "power", True]])


def test_scene_names_enum_when_configured():
    _, _, s = schema_of(scene_names=("Good night", "Movie"))
    assert valid(s, [["n", "Movie"]]) and not valid(s, [["n", "Nope"]])


def test_schema_and_validator_agree_on_a_grid():
    r, a, s = schema_of()
    eps = list(r.devices) + ["ep_dead0000"]
    caps = ["power", "speed", "lock", "brightness", "position", "temperature"]
    vals = [True, False, 0, 1, 5, 50, 100, 101, "locked", "unlocked", "x"]
    for ep, cap, v in itertools.product(eps, caps, vals):
        call = {"endpoint_id": ep, "capability": cap, "value": v}
        in_schema = valid(s, [["s", a.alias(ep), cap, v]])
        err, _ = validate_call(r, "set_state", call)
        assert in_schema == (err is None), (call, err)


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
    json.dumps(build_plan_schema(r, build_aliases(r)))               # serialisable for llama-server


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
