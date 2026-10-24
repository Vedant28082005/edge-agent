"""HomeBench LLM format (config `llm_format: "homebench"`, default stays "json"): drives the fine-tuned model exactly
as it was trained -- one system-role message (header + `<home_state>` + `<device_method>` + the per-turn trailer),
no grammar/constrained decoding, and a comma-separated reply the model learned to produce. Everything downstream of
the parser (guards, confirmation, validation) is unchanged: this module only replaces the prompt text and the reply
decoding, never the internal `{"tool": str, "args": dict}` call shape the rest of the agent already works with.

Registry-driven method table (`build_table`): each kept endpoint's actuator capabilities map to a HomeBench
`room.device.method(arg:type)` via `CAP_METHODS`, the one place that knows e.g. brightness -> set_brightness. A
capability absent from `CAP_METHODS` is left out of `<device_method>` (but still shown as a read-only attribute in
`<home_state>` when its value is known). An endpoint with `risk == "confirm"` is left out of BOTH blocks entirely --
it keeps the confirmation flow through the normal (json-format) path, never reachable from a homebench reply.

Byte-stability trade-off (asked for in the brief): `<device_method>` and the structural part of `<home_state>` (room/
device/attribute names, ranges, options) are a pure function of registry structure, identical across turns until a
device is added/removed/re-typed -- same cache story as the json format's device list. But `<home_state>` also
carries live attribute VALUES, which this project deliberately keeps OUT of the prompt elsewhere (`prompt.py`'s
device list carries "NO live state") because the model is expected to call `get_state`/`read_sensor` instead. HomeBench
was trained on the opposite assumption: full live state up front, no state-reading tool at all. Reconciling the two
means every `build_prompt_text` call takes a `states` snapshot and bakes it into the one cacheable system message, so
ANY state change changes the prefix bytes and invalidates llama.cpp's prefix (KV) cache for slot 0. Two ways out:
 (a) omit volatile values from the prefix (`state: unknown`, no attribute values) and accept the model was not
     trained for that -- cheap, but the model answers state questions blind; or
 (b) rebuild the prefix (and re-warm it, exactly the mechanism `service.py` already uses on a registry change) every
     time the supplied `states` snapshot differs from the last build.
This module takes (b): `states` is part of what callers must treat as "prefix-dirtying", same as a registry change.
Measuring the resulting cache-hit rate against real traffic needs the real llama-server + Pi (see services/agent's
test plan); unit tests here only check the text/bytes a given (registry, states) pair produce."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .devices import Cap, Device, DeviceRegistry

HEADER = (
    "You are 'Al', a helpful AI Assistant that controls the devices in a house. Complete the following task as "
    "instructed or answer the following question with the information provided only. The current status of the "
    "device and the methods it possesses are provided below, please only use the methods provided. Output "
    '"error_input" when operating non-existent attributes and devices. Only output machine instructions and '
    "enclose them in {}."
)

HOME_STATE_INTRO = (
    "The following provides the status of all devices in each room of the current household, the adjustable "
    "attributes of each device, and the threshold values for adjustable attributes:"
)
DEVICE_METHOD_INTRO = "The following provides the methods to control each device in the current household:"

TRAILER = (
    "-------------------------------\n"
    "Here are the user instructions you need to reply to.\n"
    "<User instructions:>\n"
    "{text}\n"
    "<Machine instructions:>"
)

ERROR_INPUT = "error_input"

# capability id -> (method, arg_name, arg_type) ; power is handled separately (two zero-arg methods).
CAP_METHODS: dict[str, tuple[str, str, str]] = {
    "brightness": ("set_brightness", "brightness", "int"),
    "position": ("set_degree", "degree", "int"),
    "speed": ("set_speed", "speed", "int"),
    "target_temp": ("set_temperature", "temperature", "float"),
    "mode": ("set_mode", "mode", "string"),
    "color_temp": ("set_color_temp", "color_temp", "int"),
    "lock": ("set_lock", "lock", "string"),
}
_ARG_TYPE_PY = {"int": int, "float": float}


def _snake(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s.strip().lower())
    return re.sub(r"_+", "_", s).strip("_") or "x"


@dataclass(frozen=True)
class MethodEntry:
    endpoint_id: str
    capability: str                 # None-equivalent not needed: power splits into two entries, value baked in
    room: str
    device: str
    method: str
    arg_name: str | None            # None for turn_on/turn_off (zero-arg)
    arg_type: str                   # "int" | "float" | "string" | "bool"
    cap: Cap | None                 # the live Cap, for range/enum validation; None for the power on/off entries
    fixed_value: Any = None         # for turn_on/turn_off: True/False, applied regardless of args


@dataclass(frozen=True)
class HomebenchTable:
    entries: tuple[MethodEntry, ...]
    by_name: dict[tuple[str, str, str], MethodEntry]   # (room, device, method) -> entry
    labels: dict[str, tuple[str, str]]                  # endpoint_id -> (room, device) -- also for kept sensor-only eps
    kept: tuple[Device, ...]                            # endpoints in scope for the prompt (risk != confirm)


def _device_labels(devices: list[Device]) -> dict[str, tuple[str, str]]:
    """room/device label per kept endpoint, snake_case, disambiguated by a _2/_3/... suffix when a room has more
    than one endpoint of the same type (HomeBench's own convention uses the type as the device name)."""
    by_room_type: dict[tuple[str, str], list[Device]] = {}
    for d in sorted(devices, key=lambda d: (d.room.lower(), d.type, d.endpoint_id)):
        by_room_type.setdefault((d.room, d.type), []).append(d)
    labels: dict[str, tuple[str, str]] = {}
    for (room, typ), ds in by_room_type.items():
        r = _snake(room)
        for i, d in enumerate(ds, start=1):
            labels[d.endpoint_id] = (r, _snake(typ) if i == 1 else f"{_snake(typ)}_{i}")
    return labels


def build_table(reg: DeviceRegistry) -> HomebenchTable:
    kept = [d for d in reg.devices.values() if d.risk != "confirm"]
    labels = _device_labels(kept)
    entries: list[MethodEntry] = []
    by_name: dict[tuple[str, str, str], MethodEntry] = {}

    def add(room: str, device: str, method: str, arg_name: str | None, arg_type: str, cap: Cap | None,
            ep: str, capability: str, fixed_value: Any = None) -> None:
        e = MethodEntry(ep, capability, room, device, method, arg_name, arg_type, cap, fixed_value)
        entries.append(e)
        by_name[(room, device, method)] = e

    for d in sorted(kept, key=lambda d: d.endpoint_id):
        room, device = labels[d.endpoint_id]
        for c in d.caps:
            if c.kind != "actuator":
                continue
            if c.id == "power" and c.type == "bool":
                add(room, device, "turn_on", None, "bool", None, d.endpoint_id, "power", True)
                add(room, device, "turn_off", None, "bool", None, d.endpoint_id, "power", False)
                continue
            spec = CAP_METHODS.get(c.id)
            if spec is None:
                continue
            method, arg_name, arg_type = spec
            add(room, device, method, arg_name, arg_type, c, d.endpoint_id, c.id)
    return HomebenchTable(tuple(entries), by_name, labels, tuple(kept))


# --------------------------------------------------------------------------------------------- prompt text
def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def _attr_line(cap: Cap, value: Any) -> str:
    shown = _fmt_value(value) if value is not None else "unknown"
    if cap.type == "enum" and cap.values:
        return f"    {cap.id}: {shown} (options{list(cap.values)})"
    if cap.min is not None and cap.max is not None:
        lo = int(cap.min) if float(cap.min).is_integer() else cap.min
        hi = int(cap.max) if float(cap.max).is_integer() else cap.max
        return f"    {cap.id}: {shown} (range: {lo} - {hi})"
    return f"    {cap.id}: {shown}"


def build_home_state(table: HomebenchTable, states: dict[str, dict[str, Any]] | None) -> str:
    states = states or {}
    by_room: dict[str, list[Device]] = {}
    for d in table.kept:
        by_room.setdefault(table.labels[d.endpoint_id][0], []).append(d)
    out = ["<home_state>", HOME_STATE_INTRO]
    for room in sorted(by_room):
        out.append(f"{room}:")
        for d in sorted(by_room[room], key=lambda d: table.labels[d.endpoint_id][1]):
            _, device = table.labels[d.endpoint_id]
            dvals = states.get(d.endpoint_id) or {}
            out.append(f"  {device}:")
            power = d.cap("power")
            if power is not None and power.kind == "actuator" and power.type == "bool":
                v = dvals.get("power")
                out.append(f"    state: {'on' if v else 'off' if v is not None else 'unknown'}")
            else:
                out.append("    state: unknown")
            for c in d.caps:
                if c.id == "power":
                    continue
                if c.kind == "actuator" and c.id not in CAP_METHODS:
                    continue              # not controllable via homebench -> not an "adjustable attribute"
                out.append(_attr_line(c, dvals.get(c.id)))
    out.append("</home_state>")
    return "\n".join(out)


def build_device_method(table: HomebenchTable) -> str:
    lines = ["<device_method>", DEVICE_METHOD_INTRO]
    parts = []
    for e in table.entries:
        arg = f"{e.arg_name}:{e.arg_type}" if e.arg_name else ""
        parts.append(f"{e.room}.{e.device}.{e.method}({arg});")
    lines.append("".join(parts))
    lines.append("</device_method>")
    return "\n".join(lines)


def build_prefix(reg: DeviceRegistry, states: dict[str, dict[str, Any]] | None = None) -> tuple[str, HomebenchTable]:
    """The byte-stable-per-(registry,states) prefix: header + <home_state> + <device_method>. See module docstring
    for the cache trade-off `states` introduces."""
    table = build_table(reg)
    text = "\n\n".join([HEADER, build_home_state(table, states), build_device_method(table)])
    return text, table


def build_trailer(user_text: str) -> str:
    return TRAILER.format(text=user_text)


# --------------------------------------------------------------------------------------------- reply parsing
def _split_top_level(text: str) -> list[str]:
    """Split on commas at paren depth 0 (arguments may contain tuples), tolerating an outer `{...}` wrapper."""
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    out: list[str] = []
    depth, cur = 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out]


_CALL_RE = re.compile(r"^([a-z0-9_]+)\.([a-z0-9_]+)\.([a-z0-9_]+)\((.*)\)$")


def _parse_arg(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if ":" in raw:                       # set_x(intensity:40) -- named arg, value is after the colon
        raw = raw.split(":", 1)[1].strip()
    return raw.strip("'\"")


def _coerce(entry: MethodEntry, raw: str | None) -> tuple[bool, Any]:
    """(ok, value). Validates range/enum from the registry; never guesses a value out of range or of the wrong type."""
    if entry.arg_name is None:
        return True, entry.fixed_value
    if raw is None:
        return False, None
    cap = entry.cap
    if entry.arg_type in ("int", "float"):
        try:
            v = _ARG_TYPE_PY[entry.arg_type](raw)
        except (TypeError, ValueError):
            return False, None
        if cap is not None and cap.min is not None and cap.max is not None and not (cap.min <= v <= cap.max):
            return False, None
        return True, v
    if entry.arg_type == "string":
        if cap is not None and cap.type == "enum" and cap.values and raw not in cap.values:
            return False, None
        return True, raw
    return False, None


@dataclass
class ParseResult:
    calls: list[dict]
    failures: list[str] = field(default_factory=list)   # one entry per part that could not be done


def parse_reply(text: str, table: HomebenchTable) -> ParseResult:
    calls: list[dict] = []
    failures: list[str] = []
    for part in _split_top_level(text or ""):
        piece = part.strip()
        if not piece:
            continue                       # drop empty trailing pieces (trailing comma)
        if piece.strip("{}").strip() == ERROR_INPUT:
            failures.append("could not do this part")
            continue
        m = _CALL_RE.match(piece)
        if not m:
            failures.append(f"could not understand '{piece}'")
            continue
        room, device, method, argtext = m.groups()
        entry = table.by_name.get((room, device, method))
        if entry is None:
            failures.append(f"unknown method for '{piece}'")
            continue
        raw_arg = _parse_arg(argtext) if argtext.strip() else None
        ok, value = _coerce(entry, raw_arg)
        if not ok:
            failures.append(f"invalid value for '{piece}'")
            continue
        calls.append({"tool": "set_state", "args": {"endpoint_id": entry.endpoint_id, "capability": entry.capability,
                                                     "value": value}})
    return ParseResult(calls, failures)


def render_calls(calls: list[dict], table: HomebenchTable) -> str:
    """The inverse of `parse_reply`, used only to re-show the model's own previous attempt in a retry note (kept in
    the model's own notation, never the json-format's aliased tuples)."""
    by_ep: dict[str, tuple[str, str]] = table.labels
    out = []
    for c in calls:
        a = c.get("args") if isinstance(c, dict) else None
        if not isinstance(a, dict) or c.get("tool") != "set_state":
            continue
        ep, cap, value = a.get("endpoint_id"), a.get("capability"), a.get("value")
        label = by_ep.get(ep)
        if label is None:
            continue
        room, device = label
        if cap == "power" and isinstance(value, bool):
            out.append(f"{room}.{device}.{'turn_on' if value else 'turn_off'}()")
            continue
        spec = CAP_METHODS.get(cap)
        if spec is None:
            continue
        method, arg_name, _ = spec
        out.append(f"{room}.{device}.{method}({value})")
    return ",".join(out) + ("," if out else "")
