"""(S8g) Generic power-dependency rule: setting any non-power, non-lock actuator capability to an "on" value on an
endpoint that has a boolean `power` capability must turn it on first -- driven only by the endpoint's announced
capabilities, never a device name or type. Parameterised over synthetic endpoints (a light, a fan, a curtain, a
thermostat) so a brand new device type is covered with no agent change, plus the one shared function used by both
the fast path (resolver.py) and the LLM path (chat.py's repair_plan_calls)."""
from __future__ import annotations

import pytest

from agent.devices import DeviceRegistry
from agent.schema import ensure_power_dependency

EP = "ep_aaaaaaaa"


def _cap(id_: str, kind: str, type_: str, **kw) -> dict:
    return {"id": id_, "kind": kind, "type": type_, **kw}


def _reg(caps: list[dict], risk: str = "normal") -> DeviceRegistry:
    r = DeviceRegistry()
    r.replace([{"endpoint_id": EP, "name": "Thing", "room": "Room", "type": "generic", "tags": [],
                "online": True, "risk": risk, "capabilities": caps}], 1)
    return r


def _set(cap: str, value) -> dict:
    return {"tool": "set_state", "args": {"endpoint_id": EP, "capability": cap, "value": value}}


POWER = _cap("power", "actuator", "bool")

# (capability id, announced cap, "on" value) -- light brightness, fan speed, curtain position, thermostat
# target_temp and mode: never hard-coded to one capability or device type.
ON_CASES = [
    ("brightness", _cap("brightness", "actuator", "int", min=0, max=100), 40),
    ("speed", _cap("speed", "actuator", "int", min=1, max=5), 3),
    ("position", _cap("position", "actuator", "int", min=0, max=100), 80),
    ("target_temp", _cap("target_temp", "actuator", "float", min=10, max=30), 21.5),
    ("mode", _cap("mode", "actuator", "enum", values=["cool", "heat", "off"]), "cool"),
]


@pytest.mark.parametrize("cap_id,cap,value", ON_CASES, ids=[c[0] for c in ON_CASES])
def test_power_is_inserted_before_any_on_like_non_power_capability(cap_id, cap, value):
    reg = _reg([POWER, cap])
    out = ensure_power_dependency([_set(cap_id, value)], reg)
    assert out == [_set("power", True), _set(cap_id, value)]


@pytest.mark.parametrize("cap_id,cap,off_value", [
    ("brightness", _cap("brightness", "actuator", "int", min=0, max=100), 0),
    ("speed", _cap("speed", "actuator", "int", min=1, max=5), 0),
    ("position", _cap("position", "actuator", "int", min=0, max=100), 0),
    ("mode", _cap("mode", "actuator", "enum", values=["cool", "off"]), "off"),
], ids=["brightness", "speed", "position", "mode"])
def test_power_never_inserted_for_an_off_like_value(cap_id, cap, off_value):
    reg = _reg([POWER, cap])
    calls = [_set(cap_id, off_value)]
    assert ensure_power_dependency(calls, reg) == calls


def test_power_never_inserted_for_lock():
    reg = _reg([POWER, _cap("lock", "actuator", "enum", values=["locked", "unlocked"])])
    calls = [_set("lock", "unlocked")]
    assert ensure_power_dependency(calls, reg) == calls


def test_power_never_inserted_when_the_endpoint_has_no_power_capability():
    reg = _reg([_cap("position", "actuator", "int", min=0, max=100)])
    calls = [_set("position", 50)]
    assert ensure_power_dependency(calls, reg) == calls


def test_power_never_inserted_for_a_risk_confirm_endpoint():
    reg = _reg([POWER, _cap("speed", "actuator", "int", min=1, max=5)], risk="confirm")
    calls = [_set("speed", 3)]
    assert ensure_power_dependency(calls, reg) == calls      # that endpoint keeps its own confirmation flow


def test_power_never_overrides_an_explicit_power_call_even_off():
    reg = _reg([POWER, _cap("speed", "actuator", "int", min=1, max=5)])
    calls = [_set("power", False), _set("speed", 3)]
    assert ensure_power_dependency(calls, reg) == calls


def test_power_inserted_once_per_endpoint_for_several_dependent_calls():
    reg = _reg([POWER, _cap("brightness", "actuator", "int", min=0, max=100),
                _cap("color_temp", "actuator", "int", min=2700, max=6500)])
    out = ensure_power_dependency([_set("brightness", 60), _set("color_temp", 4000)], reg)
    assert out == [_set("power", True), _set("brightness", 60), _set("color_temp", 4000)]


def test_unknown_endpoint_is_left_alone():
    reg = _reg([POWER, _cap("speed", "actuator", "int", min=1, max=5)])
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_deadbeef", "capability": "speed", "value": 3}}]
    assert ensure_power_dependency(calls, reg) == calls
