"""Constrained decoding: a JSON schema generated from the LIVE registry, plus the executor's own validator.

Approach (documented in README): llama.cpp's `response_format: {type: json_schema}` turns the schema into a GBNF grammar
that is enforced token by token. We constrain the planner's whole reply (`{"calls": [...], "say": "..."}`) rather than
using llama.cpp's native `tools` mode: native tool calls depend on the chat template and on lazily triggered grammars,
which cannot guarantee an enum of endpoint ids. With this schema a non-existent endpoint, a capability the endpoint does
not have, or an out-of-range value cannot be produced. The executor re-validates every call anyway
(`validate_call`), so unconstrained mode (the S10 ablation) is safe too: invalid calls are rejected, and counted.

Size: set_state variants are grouped by (capability, value-schema), each with an enum of the endpoints that have that
capability, so the grammar grows with the number of distinct capability shapes, not with the number of devices."""
from __future__ import annotations

import json
import re
from typing import Any

from .devices import ENDPOINT_TYPES, DeviceRegistry
from .hub_client import TOOLS

MAX_CALLS = 4
SAY_MAX = 240
FORBIDDEN_ARG_KEYS = ("confirmation_token", "user_id", "call_id", "_meta", "token")
SCENE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.'&-]{0,47}$")
BRIGHTNESS_FLOOR = 10
_LEVEL_NAMED_RE = re.compile(r"\d|\bzero\b|\bpercent\b|%", re.IGNORECASE)


def _call(tool: str, args: dict, required: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["tool", "args"],
            "properties": {"tool": {"const": tool},
                           "args": {"type": "object", "additionalProperties": False, "required": required,
                                    "properties": args}}}


def build_plan_schema(reg: DeviceRegistry, scene_names: tuple[str, ...] | None = None) -> dict:
    variants: list[dict] = []
    # set_state: one variant per (capability, value schema), enum of endpoints having it
    groups: dict[tuple[str, str], tuple[dict, list[str]]] = {}
    for d, c in sorted(reg.actuators(), key=lambda t: (t[0].endpoint_id, t[1].id)):
        vs = c.value_schema()
        if vs is None:
            continue
        key = (c.id, json.dumps(vs, sort_keys=True))
        groups.setdefault(key, (vs, []))[1].append(d.endpoint_id)
    for (cap, _), (vs, eps) in sorted(groups.items()):
        variants.append(_call("set_state", {"endpoint_id": {"enum": eps}, "capability": {"const": cap}, "value": vs},
                              ["endpoint_id", "capability", "value"]))
    # read_sensor
    sgroups: dict[str, list[str]] = {}
    for d, c in sorted(reg.sensors(), key=lambda t: (t[0].endpoint_id, t[1].id)):
        sgroups.setdefault(c.id, []).append(d.endpoint_id)
    for cap, eps in sorted(sgroups.items()):
        variants.append(_call("read_sensor", {"endpoint_id": {"enum": eps}, "capability": {"const": cap}},
                              ["endpoint_id", "capability"]))
    # get_state / list_devices
    if reg.devices:
        variants.append(_call("get_state", {"endpoint_id": {"enum": sorted(reg.devices)}}, ["endpoint_id"]))
        variants.append(_call("get_state", {"room": {"enum": reg.rooms()}}, ["room"]))
        lprops: dict[str, Any] = {"room": {"enum": reg.rooms()},
                                  "type": {"enum": sorted({d.type for d in reg.devices.values()})}}
        variants.append(_call("list_devices", lprops, []))
    if scene_names is None:
        scene_names = reg.scene_names()              # from list_devices.scenes (already scoped for a scoped view)
    scene = {"enum": list(scene_names)} if scene_names else {"type": "string", "pattern": SCENE_RE.pattern, "maxLength": 48}
    variants.append(_call("run_scene", {"scene": scene}, ["scene"]))
    calls: dict = {"type": "array", "maxItems": MAX_CALLS, "items": {"anyOf": variants}}
    return {"type": "object", "additionalProperties": False, "required": ["calls", "say"],
            "properties": {"calls": calls, "say": {"type": "string", "maxLength": SAY_MAX}}}


def schema_endpoint_ids(schema: dict) -> set[str]:
    """Every endpoint id the schema can express (used by tests and the ablation counters)."""
    out: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if "endpoint_id" in n and isinstance(n["endpoint_id"], dict):
                out.update(n["endpoint_id"].get("enum", []))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(schema)
    return out


# --------------------------------------------------------------------------------------------- plan repair (pre-executor)
def repair_plan_calls(calls: list[dict], user_text: str, reg: DeviceRegistry) -> list[dict]:
    """Two independent repairs applied to every plan (LLM or fast path) before it reaches the executor: the brightness
    floor (S8e), then the generic power-dependency rule (S8g, see `ensure_power_dependency`)."""
    return ensure_power_dependency(_lift_brightness_floor(calls, user_text), reg)


def _lift_brightness_floor(calls: list[dict], user_text: str) -> list[dict]:
    """A `set_state brightness=0` is never sent as-is unless the user named a level or `0`/"zero" themselves: turning a
    light on must never leave it dark. An explicit `power=false` call for the same endpoint in this plan is a genuine
    turn-off (brightness is moot), so it is left alone; everything else is repaired to `BRIGHTNESS_FLOOR`, never applied
    silently."""
    if _LEVEL_NAMED_RE.search(user_text or ""):
        return calls
    power_false_eps = {c["args"].get("endpoint_id") for c in calls if isinstance(c, dict) and isinstance(c.get("args"), dict)
                        and c.get("tool") == "set_state" and c["args"].get("capability") == "power"
                        and c["args"].get("value") is False}
    out = []
    for c in calls:
        a = c.get("args") if isinstance(c, dict) else None
        if (isinstance(a, dict) and c.get("tool") == "set_state" and a.get("capability") == "brightness"
                and a.get("value") == 0 and a.get("endpoint_id") not in power_false_eps):
            c = {**c, "args": {**a, "value": BRIGHTNESS_FLOOR}}
        out.append(c)
    return out


# Actuator capabilities whose own meaning already covers the endpoint's on/off state, so this rule never touches
# them: `power` is the dependency itself, `lock` is a security state unrelated to electrical power.
POWER_INDEPENDENT_CAPS = ("power", "lock")


def _is_off_like(value: object) -> bool:
    """A value that itself means "off" (or the zero/false floor of a range) never implies turning the endpoint on;
    a genuine dim-to-off / move-to-0 / mode-"off" is left alone."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.lower() == "off"
    return False


def ensure_power_dependency(calls: list[dict], reg: DeviceRegistry) -> list[dict]:
    """(S8g) Generic rule, driven only by the endpoint's announced capabilities (never a device name or type): setting
    any non-power, non-lock actuator capability (int level, enum mode, position, float setpoint, ...) to an "on"-like
    value on an endpoint that has a boolean `power` capability must turn that endpoint on first. Replaces the old
    brightness-only `_ensure_power_for_brightness` (S8f); same ordering and non-override guarantees, now generic:

    - Skipped when the value itself is off-like (0, False, "off"): never turns a light on by dimming it to 0.
    - Skipped when this plan already states power for that endpoint (explicit, or already inserted for an earlier
      call on the same endpoint): this never overrides an explicit power call, it only fills in one that is missing.
    - Skipped when the endpoint is `risk: confirm`: that endpoint keeps its own confirmation flow untouched; this
      rule never adds an unconfirmed call ahead of a risky one.
    - Skipped when the endpoint has no boolean `power` actuator capability at all (e.g. a sensor-only endpoint, or
      a device type that was never given a power switch).

    The power call always precedes the dependent call for the same endpoint."""
    power_eps = {c["args"].get("endpoint_id") for c in calls if isinstance(c, dict) and isinstance(c.get("args"), dict)
                 and c.get("tool") == "set_state" and c["args"].get("capability") == "power"}
    out: list[dict] = []
    for c in calls:
        a = c.get("args") if isinstance(c, dict) else None
        if (isinstance(a, dict) and c.get("tool") == "set_state" and isinstance(a.get("capability"), str)
                and a["capability"] not in POWER_INDEPENDENT_CAPS and not _is_off_like(a.get("value"))
                and isinstance(a.get("endpoint_id"), str) and a["endpoint_id"] not in power_eps):
            ep = a["endpoint_id"]
            d = reg.devices.get(ep)
            p = d.cap("power") if d else None
            if d is not None and p is not None and p.kind == "actuator" and p.type == "bool" and d.risk != "confirm":
                out.append({"tool": "set_state", "args": {"endpoint_id": ep, "capability": "power", "value": True}})
                power_eps.add(ep)
        out.append(c)
    return out


# --------------------------------------------------------------------------------------------- executor validation
def strip_forbidden(args: dict) -> tuple[dict, list[str]]:
    """Out-of-band fields (token, user, call id) in the model's own arguments are removed and reported (names only)."""
    removed = [k for k in args if k in FORBIDDEN_ARG_KEYS]
    return {k: v for k, v in args.items() if k not in FORBIDDEN_ARG_KEYS}, removed


def validate_call(reg: DeviceRegistry, tool: Any, args: Any, scene_names: tuple[str, ...] | None = None) -> tuple[str | None, dict]:
    """(error_code | None, cleaned args). Same rules the hub applies, checked against the live registry BEFORE anything
    reaches hardware. Error codes mirror the hub's where one exists."""
    if tool not in TOOLS:
        return "unknown_tool", {}
    if not isinstance(args, dict):
        return "bad_args", {}
    a, _ = strip_forbidden(args)

    def only(*keys: str) -> bool:
        return set(a) <= set(keys)

    if tool == "set_state":
        if set(a) != {"endpoint_id", "capability", "value"}:
            return "bad_args", {}
        d = reg.devices.get(a["endpoint_id"]) if isinstance(a["endpoint_id"], str) else None
        if d is None:
            return "unknown_endpoint", {}
        c = d.cap(a["capability"]) if isinstance(a["capability"], str) else None
        if c is None:
            return "unknown_capability", {}
        if c.kind != "actuator":
            return "not_actuator", {}
        v = a["value"]
        if c.type == "bool":
            return (None, a) if isinstance(v, bool) else ("bad_type", {})
        if c.type == "enum":
            return (None, a) if isinstance(v, str) and v in c.values else ("bad_type", {})
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "bad_type", {}
        if c.type == "int":
            if isinstance(v, float):
                if not v.is_integer():
                    return "bad_type", {}
                v = int(v)
            a = {**a, "value": v}
        if c.min is None or c.max is None or not (c.min <= v <= c.max):
            return "out_of_range", {}
        return None, a
    if tool == "get_state":
        if len(a) != 1 or not only("endpoint_id", "room"):
            return "bad_args", {}
        if "endpoint_id" in a:
            return (None, a) if a["endpoint_id"] in reg.devices else ("unknown_endpoint", {})
        return (None, a) if a["room"] in reg.rooms() else ("unknown_room", {})
    if tool == "read_sensor":
        if set(a) != {"endpoint_id", "capability"}:
            return "bad_args", {}
        d = reg.devices.get(a["endpoint_id"]) if isinstance(a["endpoint_id"], str) else None
        if d is None:
            return "unknown_endpoint", {}
        c = d.cap(a["capability"]) if isinstance(a["capability"], str) else None
        if c is None:
            return "unknown_capability", {}
        return (None, a) if c.kind == "sensor" else ("not_sensor", {})
    if tool == "run_scene":
        if set(a) != {"scene"} or not isinstance(a["scene"], str):
            return "bad_args", {}
        if scene_names:                                  # explicit closed list (tests, callers that know better than the cache)
            return (None, a) if a["scene"] in scene_names else ("unknown_scene", {})
        if reg.find_scene(a["scene"]) is not None:
            return None, a
        # Not in our (possibly stale) scene listing: a scene created since the last reload is still the hub's call to make,
        # so this stays a shape check. Scoped callers never get here: the executor's scope check refuses it first.
        return (None, a) if SCENE_RE.match(a["scene"]) else ("bad_args", {})
    # list_devices
    if not only("room", "type"):
        return "bad_args", {}
    if "room" in a and a["room"] not in reg.rooms():
        return "unknown_room", {}
    if "type" in a and a["type"] not in ENDPOINT_TYPES:
        return "bad_args", {}
    return None, a
