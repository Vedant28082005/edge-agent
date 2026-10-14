"""A registry rich enough for ambiguity cases (two fans, two ceiling lights, a risk:confirm lock, a power-only light)."""
from __future__ import annotations

from agent.devices import DeviceRegistry


def _cap(cid, kind, typ, **kw):
    return {"id": cid, "kind": kind, "type": typ, **kw}


POWER = _cap("power", "actuator", "bool")
BRIGHT = _cap("brightness", "actuator", "int", min=0, max=100)

DEVICES = [
    {"endpoint_id": "ep_b0000001", "name": "Ceiling light", "room": "Bedroom", "type": "light", "tags": ["night"],
     "online": True, "risk": "normal", "capabilities": [POWER, BRIGHT]},
    {"endpoint_id": "ep_b0000002", "name": "Fan", "room": "Bedroom", "type": "fan", "tags": [], "online": True,
     "risk": "normal", "capabilities": [POWER, _cap("speed", "actuator", "int", min=1, max=5)]},
    {"endpoint_id": "ep_b0000003", "name": "Temp sensor", "room": "Bedroom", "type": "sensor", "tags": [], "online": True,
     "risk": "normal", "capabilities": [_cap("temperature", "sensor", "float", unit="C")]},
    {"endpoint_id": "ep_b0000004", "name": "Lamp", "room": "Living room", "type": "light", "tags": [], "online": True,
     "risk": "normal", "capabilities": [POWER, BRIGHT]},
    {"endpoint_id": "ep_b0000005", "name": "Blinds", "room": "Living room", "type": "cover", "tags": [], "online": True,
     "risk": "normal", "capabilities": [_cap("position", "actuator", "int", min=0, max=100)]},
    {"endpoint_id": "ep_b0000006", "name": "Front door", "room": "Hall", "type": "lock", "tags": [], "online": True,
     "risk": "confirm", "capabilities": [_cap("lock", "actuator", "enum", values=["locked", "unlocked"])]},
    {"endpoint_id": "ep_b0000007", "name": "Fan", "room": "Living room", "type": "fan", "tags": [], "online": True,
     "risk": "normal", "capabilities": [POWER, _cap("speed", "actuator", "int", min=1, max=3)]},
    {"endpoint_id": "ep_b0000008", "name": "Ceiling light", "room": "Kitchen", "type": "light", "tags": [], "online": True,
     "risk": "normal", "capabilities": [POWER]},
    {"endpoint_id": "ep_b0000009", "name": "Humidity sensor", "room": "Living room", "type": "sensor", "tags": [],
     "online": True, "risk": "normal", "capabilities": [_cap("humidity", "sensor", "float", unit="%")]},
    {"endpoint_id": "ep_b000000a", "name": "Motion sensor", "room": "Hall", "type": "sensor", "tags": [], "online": True,
     "risk": "normal", "capabilities": [_cap("motion", "sensor", "bool")]},
]
SCENES = ("Good night", "Movie time", "Morning")


def registry(devices=None) -> DeviceRegistry:
    reg = DeviceRegistry()
    reg.replace(list(devices if devices is not None else DEVICES), 1)
    return reg
