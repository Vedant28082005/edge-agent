"""Live device registry cache (from the hub's list_devices + event stream). Everything a device or admin supplied is
sanitised at ingest; entries that do not match the contract shapes are dropped. No live values are cached here."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .util import ENUM_VAL_RE, EP_RE, clean_label, clean_tag

log = logging.getLogger("agent.devices")

ENDPOINT_TYPES = ("light", "fan", "outlet", "lock", "cover", "thermostat", "sensor", "generic")
# Fixed vocabulary (contracts/capabilities.md v2): id -> (kind, type, default range / values)
VOCAB: dict[str, tuple[str, str, tuple]] = {
    "power": ("actuator", "bool", ()), "brightness": ("actuator", "int", (0, 100)),
    "color_temp": ("actuator", "int", ()), "speed": ("actuator", "int", ()),
    "position": ("actuator", "int", (0, 100)), "lock": ("actuator", "enum", ("locked", "unlocked")),
    "target_temp": ("actuator", "float", ()), "mode": ("actuator", "enum", ()),
    "temperature": ("sensor", "float", ()), "humidity": ("sensor", "float", ()),
    "illuminance": ("sensor", "float", ()), "motion": ("sensor", "bool", ()),
    "contact": ("sensor", "enum", ("open", "closed")), "power_w": ("sensor", "float", ()),
    "current_a": ("sensor", "float", ()), "voltage_v": ("sensor", "float", ()),
    "battery": ("sensor", "int", (0, 100)),
}


@dataclass(frozen=True)
class Cap:
    id: str
    kind: str                       # actuator | sensor
    type: str                       # bool | int | float | enum
    min: float | None = None
    max: float | None = None
    values: tuple[str, ...] = ()
    unit: str = ""

    def value_schema(self) -> dict | None:
        """JSON schema of an acceptable set_state value. Used for the constrained-decoding grammar AND by the
        executor's own validation. None = the range is unknown, so the capability is not offered to the model."""
        if self.type == "bool":
            return {"type": "boolean"}
        if self.type == "enum":
            return {"enum": list(self.values)} if self.values else None
        if self.type == "int":
            if self.min is None or self.max is None:
                return None
            return {"type": "integer", "minimum": int(self.min), "maximum": int(self.max)}
        if self.type == "float":
            if self.min is None or self.max is None:
                return None
            return {"type": "number", "minimum": self.min, "maximum": self.max}
        return None


@dataclass(frozen=True)
class Device:
    endpoint_id: str
    name: str
    room: str
    type: str
    tags: tuple[str, ...]
    risk: str                       # normal | confirm
    online: bool
    caps: tuple[Cap, ...]

    def cap(self, cap_id: str) -> Cap | None:
        return next((c for c in self.caps if c.id == cap_id), None)


def parse_cap(raw: Any) -> Cap | None:
    if not isinstance(raw, dict):
        return None
    cid, kind = raw.get("id"), raw.get("kind")
    if cid not in VOCAB or kind not in ("actuator", "sensor") or VOCAB[cid][0] != kind:
        return None
    ctype = VOCAB[cid][1]
    lo, hi = (VOCAB[cid][2] if ctype in ("int", "float") and VOCAB[cid][2] else (None, None))
    values: tuple[str, ...] = VOCAB[cid][2] if ctype == "enum" else ()
    mn, mx = raw.get("min", lo), raw.get("max", hi)
    if ctype in ("int", "float"):
        for x in (mn, mx):
            if x is not None and (isinstance(x, bool) or not isinstance(x, (int, float))):
                return None
        if lo is not None and mn is not None and mx is not None:      # announce range must lie inside the vocabulary's
            mn, mx = max(mn, lo), min(mx, hi)
            if mn > mx:
                return None
    if ctype == "enum" and cid == "mode":
        vals = raw.get("values")
        if not isinstance(vals, list):
            return None
        values = tuple(v for v in vals if isinstance(v, str) and ENUM_VAL_RE.match(v))
    unit = raw.get("unit") if raw.get("unit") in ("C", "%", "lux", "W", "A", "V", "K", "step") else ""
    return Cap(cid, kind, ctype, mn, mx, values, unit or "")


def parse_device(raw: Any) -> Device | None:
    if not isinstance(raw, dict):
        return None
    ep = raw.get("endpoint_id")
    if not isinstance(ep, str) or not EP_RE.match(ep) or raw.get("type") not in ENDPOINT_TYPES:
        return None
    name, room = clean_label(raw.get("name")), clean_label(raw.get("room"))
    if not name or not room:
        return None
    tags_raw = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    tags = tuple(dict.fromkeys(t for t in (clean_tag(x) for x in tags_raw[:8]) if t))
    caps_raw = raw.get("capabilities") if isinstance(raw.get("capabilities"), list) else []
    caps = tuple(c for c in (parse_cap(x) for x in caps_raw[:32]) if c)
    risk = "confirm" if raw.get("risk") == "confirm" else "normal"
    return Device(ep, name, room, raw["type"], tags, risk, bool(raw.get("online")), caps)


@dataclass
class DeviceRegistry:
    devices: dict[str, Device] = field(default_factory=dict)
    version: int = -1                # hub registry version this list corresponds to
    generation: int = 0              # bumped on every reload

    def replace(self, raw_devices: list, version: int | None = None) -> None:
        devs: dict[str, Device] = {}
        for raw in raw_devices if isinstance(raw_devices, list) else []:
            d = parse_device(raw)
            if d is None:
                log.warning("dropped a malformed device entry from list_devices")
                continue
            devs[d.endpoint_id] = d
        self.devices = devs
        if version is not None:
            self.version = version
        self.generation += 1

    def mark_online(self, endpoint_ids: list[str], online: bool) -> None:
        for ep in endpoint_ids:
            d = self.devices.get(ep)
            if d:
                self.devices[ep] = Device(d.endpoint_id, d.name, d.room, d.type, d.tags, d.risk, online, d.caps)

    def rooms(self) -> list[str]:
        return sorted({d.room for d in self.devices.values()})

    def actuators(self):
        for d in self.devices.values():
            for c in d.caps:
                if c.kind == "actuator":
                    yield d, c

    def sensors(self):
        for d in self.devices.values():
            for c in d.caps:
                if c.kind == "sensor":
                    yield d, c


class RegistryLoader:
    """Reloads the cache from the hub; one reload at a time, a request during a reload triggers exactly one more."""

    def __init__(self, hub, registry: DeviceRegistry, on_changed=None) -> None:
        self.hub, self.registry, self.on_changed = hub, registry, on_changed
        self._lock = asyncio.Lock()
        self._again = False

    async def reload(self, version: int | None = None) -> None:
        if self._lock.locked():
            self._again = True
            return
        async with self._lock:
            while True:
                self._again = False
                res = await self.hub.call_tool("list_devices", {})
                if isinstance(res, dict) and isinstance(res.get("devices"), list):
                    self.registry.replace(res["devices"], version)
                    if self.on_changed:
                        self.on_changed()
                else:
                    log.warning("list_devices returned an unexpected result; keeping the previous registry")
                if not self._again:
                    return
