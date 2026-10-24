"""Constrained decoding: a JSON schema generated from the LIVE registry, plus the executor's own validator.

Approach (documented in README): llama.cpp's `response_format: {type: json_schema}` turns the schema into a GBNF grammar
that is enforced token by token. We constrain the planner's whole reply rather than using llama.cpp's native `tools`
mode: native tool calls depend on the chat template and on lazily triggered grammars, which cannot guarantee an enum of
endpoint ids. With this schema a non-existent endpoint, a capability the endpoint does not have, or an out-of-range
value cannot be produced. The executor re-validates every call anyway (`validate_call`), so unconstrained mode (the S10
ablation) is safe too: invalid calls are rejected, and counted.

Wire format (S8i, compact): the reply is a bare JSON array of up to `MAX_CALLS` short tuples, `[code, ...]`, never an
object and never free text (`TOOL_CODES`/`CODE_TOOLS`; no "say" -- the agent composes all reply text from verified
results, see `chat.compose_final` / `chat.describe_outcomes` / `chat.no_plan_text`). Each tuple names its endpoint by a
short per-turn alias (`aliases.AliasMap`, e.g. "e3"), never the full `endpoint_id`: fewer tokens to generate, and fewer
to re-evaluate on a retry round. `planner.parse_plan` maps tuples back to the internal `{"tool", "args"}` call shape
(decode) and the executor's validation covers the aliasing mistake a model could still make (the mapped-back args are
checked exactly as before). `$schema` pins draft-07 so a tuple's `items` list is validated positionally everywhere
(jsonschema `validate()` is draft-sniffing and newer drafts moved tuple validation to `prefixItems`).

Size: set_state variants are grouped by (capability, value-schema), each with an enum of the aliases that have it, so
the grammar grows with the number of distinct capability shapes, not with the number of devices."""
from __future__ import annotations

import json
import re
from typing import Any

from .aliases import AliasMap
from .devices import ENDPOINT_TYPES, DeviceRegistry
from .hub_client import TOOLS

MAX_CALLS = 4
FORBIDDEN_ARG_KEYS = ("confirmation_token", "user_id", "call_id", "_meta", "token")
SCENE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.'&-]{0,47}$")
BRIGHTNESS_FLOOR = 10
_LEVEL_NAMED_RE = re.compile(r"\d|\bzero\b|\bpercent\b|%", re.IGNORECASE)

DRAFT7 = "http://json-schema.org/draft-07/schema#"
# tool name -> wire code. get_state has two tuple shapes (by endpoint / by room), told apart by a distinct code so
# each is an unambiguous, fixed-arity variant (no optional tuple positions to encode).
TOOL_CODES = {"set_state": "s", "read_sensor": "r", "get_state_ep": "g", "get_state_room": "gr",
              "list_devices": "l", "run_scene": "n"}
CODE_TOOLS = {v: k for k, v in TOOL_CODES.items()}


def _tuple(code: str, *items: dict) -> dict:
    n = len(items) + 1
    return {"type": "array", "minItems": n, "maxItems": n, "additionalItems": False,
            "items": [{"const": code}, *items]}


def _nullable(sch: dict) -> dict:
    return {"anyOf": [sch, {"type": "null"}]}


def build_plan_schema(reg: DeviceRegistry, aliases: AliasMap, scene_names: tuple[str, ...] | None = None) -> dict:
    variants: list[dict] = []
    # set_state: one variant per (capability, value schema), enum of the aliases having it
    groups: dict[tuple[str, str], tuple[dict, list[str]]] = {}
    for d, c in sorted(reg.actuators(), key=lambda t: (t[0].endpoint_id, t[1].id)):
        vs = c.value_schema()
        if vs is None:
            continue
        key = (c.id, json.dumps(vs, sort_keys=True))
        groups.setdefault(key, (vs, []))[1].append(aliases.alias(d.endpoint_id))
    for (cap, _), (vs, als) in sorted(groups.items()):
        variants.append(_tuple(TOOL_CODES["set_state"], {"enum": sorted(als)}, {"const": cap}, vs))
    # read_sensor
    sgroups: dict[str, list[str]] = {}
    for d, c in sorted(reg.sensors(), key=lambda t: (t[0].endpoint_id, t[1].id)):
        sgroups.setdefault(c.id, []).append(aliases.alias(d.endpoint_id))
    for cap, als in sorted(sgroups.items()):
        variants.append(_tuple(TOOL_CODES["read_sensor"], {"enum": sorted(als)}, {"const": cap}))
    # get_state / list_devices
    if reg.devices:
        all_aliases = sorted(aliases.alias(ep) for ep in reg.devices)
        variants.append(_tuple(TOOL_CODES["get_state_ep"], {"enum": all_aliases}))
        variants.append(_tuple(TOOL_CODES["get_state_room"], {"enum": reg.rooms()}))
        types = sorted({d.type for d in reg.devices.values()})
        variants.append(_tuple(TOOL_CODES["list_devices"], _nullable({"enum": reg.rooms()}),
                               _nullable({"enum": types})))
    if scene_names is None:
        scene_names = reg.scene_names()              # from list_devices.scenes (already scoped for a scoped view)
    scene = {"enum": list(scene_names)} if scene_names else {"type": "string", "pattern": SCENE_RE.pattern, "maxLength": 48}
    variants.append(_tuple(TOOL_CODES["run_scene"], scene))
    return {"$schema": DRAFT7, "type": "array", "maxItems": MAX_CALLS, "items": {"anyOf": variants}}


def schema_endpoint_ids(schema: dict, aliases: AliasMap) -> set[str]:
    """Every endpoint id the schema can express (used by tests and the ablation counters): every alias enum value,
    mapped back through `aliases`."""
    out: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            for v in n.get("enum", []) if isinstance(n.get("enum"), list) else ():
                if isinstance(v, str) and v in aliases.to_ep:
                    out.add(aliases.to_ep[v])
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


# --------------------------------------------------------------------------------------------- ambient polarity (S8h)
# A vague ambient complaint ("it's dark/bright/hot/cold/stuffy in here", no device named, no number) implies a
# direction: +1 = the user wants more of the domain's effect (more light, more warmth, more airflow), -1 = less.
# Data-driven on purpose: a new word or a new domain is one table row, never a device-type branch.
AMBIENT_WORDS: dict[str, tuple[str, int]] = {
    "dark": ("light", 1), "darker": ("light", 1), "dim": ("light", 1), "dimly": ("light", 1),
    "gloomy": ("light", 1), "murky": ("light", 1), "shadowy": ("light", 1), "pitchblack": ("light", 1),
    "bright": ("light", -1), "brighter": ("light", -1), "glare": ("light", -1), "glaring": ("light", -1),
    "blinding": ("light", -1), "dazzling": ("light", -1),
    "cold": ("temp", 1), "chilly": ("temp", 1), "freezing": ("temp", 1), "frigid": ("temp", 1),
    "hot": ("temp", -1), "warm": ("temp", -1), "boiling": ("temp", -1), "sweltering": ("temp", -1),
    "stuffy": ("air", 1), "muggy": ("air", 1), "suffocating": ("air", 1), "airless": ("air", 1),
}
# domain -> the endpoint type that answers it (ARCHITECTURE device types, same convention the resolver already uses
# for its per-type capability hint, e.g. `_DEFAULT_CAP`).
AMBIENT_DOMAIN_TYPE = {"light": "light", "temp": "thermostat", "air": "fan"}


def ambient_domain_devices(reg: DeviceRegistry, domain: str) -> list:
    """Live devices that can answer a complaint in this domain (used both to size the ambiguity and, by the
    caller, to name real devices in a clarifying question)."""
    typ = AMBIENT_DOMAIN_TYPE.get(domain)
    return [d for d in reg.devices.values() if d.type == typ] if typ else []


def ambient_request_signal(user_text: str, reg: DeviceRegistry | None = None) -> tuple[str, int] | None:
    """(domain, expected direction) for a vague ambient complaint, or None when the text names an explicit level
    (a number/percent/"zero", see `_LEVEL_NAMED_RE`), names a device by its own registry name, or matches more than
    one domain at once (ambiguous, left to the model/clarifying path untouched)."""
    if _LEVEL_NAMED_RE.search(user_text or ""):
        return None
    low = (user_text or "").lower()
    if reg is not None and any(d.name.lower() in low for d in reg.devices.values()):
        return None
    toks = set(re.findall(r"[a-z']+", low))
    hits = {AMBIENT_WORDS[w] for w in toks if w in AMBIENT_WORDS}
    return next(iter(hits)) if len(hits) == 1 else None


def _call_direction(domain: str, cap: str, value: object) -> int | None:
    """Which way one set_state call moves the domain's effect, or None when this call says nothing about it. Keyed
    only on capability id + value (never a device name), so it generalises to any endpoint announcing these caps."""
    if domain == "light":
        if cap == "power" and isinstance(value, bool):
            return 1 if value else -1
        if cap == "brightness":
            return -1 if _is_off_like(value) else 1
    elif domain == "air":
        if cap == "power" and isinstance(value, bool):
            return 1 if value else -1
        if cap == "speed":
            return -1 if _is_off_like(value) else 1
    elif domain == "temp":
        if cap == "mode" and value in ("heat", "cool"):
            return 1 if value == "heat" else -1
    return None


def plan_domain_direction(domain: str, calls: list[dict]) -> int | None:
    """The single direction this plan moves `domain` in, or None when no call speaks to it or the calls disagree
    (treated as ambiguous, never guessed)."""
    dirs = set()
    for c in calls:
        a = c.get("args") if isinstance(c, dict) else None
        if not isinstance(a, dict) or c.get("tool") != "set_state" or not isinstance(a.get("capability"), str):
            continue
        d = _call_direction(domain, a["capability"], a.get("value"))
        if d is not None:
            dirs.add(d)
    return next(iter(dirs)) if len(dirs) == 1 else None


def contradicts_ambient_request(calls: list[dict], user_text: str, reg: DeviceRegistry) -> bool:
    """True when the plan's calls move exactly opposite to what an ambient complaint in `user_text` asked for."""
    sig = ambient_request_signal(user_text, reg)
    if sig is None or not calls:
        return False
    domain, expected = sig
    actual = plan_domain_direction(domain, calls)
    return actual is not None and actual == -expected


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
