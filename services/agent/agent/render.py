"""Pure text-rendering helpers shared by the chat turn loop (chat.py) and the scheduler's own fast paths
(sched/scheduler.py). Kept out of chat.py so the scheduler -- which chat.py itself imports -- can reuse the exact
same wording without a circular import. Every function here builds text only from verified device/registry data
and `CallOutcome`s, never from anything the model said (S8f/S8i): the model has no text channel at all."""
from __future__ import annotations

from .devices import DeviceRegistry
from .executor import CallOutcome
from .schema import ambient_domain_devices
from .util import clean_label


def name(reg: DeviceRegistry, ep: object) -> str:
    d = reg.devices.get(ep) if isinstance(ep, str) else None
    return d.name if d else "that device"


def fmt_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return clean_label(v, 24) or "?"


def join_names(names: list[str], limit: int = 8) -> str:
    """"A", "A and B", "A, B and C"; past `limit` the rest is counted ("A, B, ... and 2 more"). A short list is never
    cut: "and 1 more" is not an answer."""
    names = list(names)
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" and {len(names) - limit} more"
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def which_one_text(devs: list) -> str:
    """The clarifying question for a singular reference that matches several devices: real names, with the room only
    when two of them share a name."""
    names = [d.name for d in devs]
    dup = {n for n in names if names.count(n) > 1}
    labels = [f"{d.name} ({d.room})" if d.name in dup else d.name for d in devs]
    if len(labels) > 6:
        return f"Which one do you mean: {join_names(labels, 6)}?"
    return "Which one do you mean: " + (", ".join(labels[:-1]) + " or " + labels[-1]) + "?"


def describe_outcomes(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """The confirmation text for a pure action turn, built from the verified outcomes (S8f)."""
    acted = [o for o in outs if o.ok and o.tool in ("set_state", "run_scene")]
    if not acted:
        return ""
    on_eps = {o.args.get("endpoint_id") for o in acted if o.tool == "set_state" and o.args.get("capability") == "power"
              and o.args.get("value") is True}
    # (S8g) generic: any other set_state on the same endpoint in this turn is narrated together with the power-on,
    # not as a separate "turned on." sentence -- whatever capability it is, not just brightness.
    other_cap_eps = {o.args.get("endpoint_id") for o in acted if o.tool == "set_state" and o.args.get("capability") != "power"}
    parts: list[str] = []
    for o in acted:
        if o.tool == "run_scene":
            parts.append(f"Running {o.args.get('scene')}.")
            continue
        cap, val, ep = o.args.get("capability"), o.args.get("value"), o.args.get("endpoint_id")
        nm = name(reg, ep)
        if cap == "power":
            if val is True and ep in other_cap_eps:
                continue                          # narrated together with the dependent call below
            parts.append(f"{nm} turned {'on' if val else 'off'}.")
        elif cap == "lock":
            parts.append(f"{nm} {'unlocked' if val == 'unlocked' else 'locked'}.")
        elif cap == "position":
            parts.append(f"{nm} {'opened' if val else 'closed'}.")
        elif cap == "brightness":
            if ep in on_eps:
                parts.append(f"{nm} turned on at {val}%.")
            else:
                parts.append(f"{nm} brightness set to {val}.")
        elif ep in on_eps:
            parts.append(f"{nm} turned on; {cap} set to {val}.")
        else:
            parts.append(f"{nm} {cap} set to {val}.")
    return " ".join(parts)


def describe_reads(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """Text for a pure-read turn (read_sensor / get_state), built from the verified results: the only place a
    read_sensor/get_state value ever reaches the user. Values come straight from the hub's own result fields, never
    a model draft, and are re-sanitised (`clean_label`) like every other device-derived string."""
    if any(o.ok and o.tool == "list_devices" for o in outs):
        return describe_list(outs, reg)
    reads = [o for o in outs if o.ok and o.tool in ("read_sensor", "get_state")]
    if not reads:
        return ""
    parts: list[str] = []
    for o in reads:
        if o.tool == "read_sensor":
            nm = name(reg, o.args.get("endpoint_id"))
            cap = clean_label(o.result.get("capability"), 24)
            unit = clean_label(o.result.get("unit"), 8)
            stale = " (last known)" if o.result.get("stale") else ""
            parts.append(f"{nm} {cap}: {fmt_value(o.result.get('value'))}{unit}{stale}.")
        else:
            for s in o.result.get("states") or []:
                if not isinstance(s, dict):
                    continue
                nm = name(reg, s.get("endpoint_id"))
                if not s.get("online"):
                    parts.append(f"{nm}: offline.")
                    continue
                vs = ", ".join(f"{clean_label(k, 24)}={fmt_value(v)}" for k, v in sorted((s.get("values") or {}).items()))
                parts.append(f"{nm}: {vs}." if vs else f"{nm}: no data.")
    return " ".join(parts)


LIST_MAX = 8
_LIST_LEVELS = (("brightness", "%"), ("speed", ""), ("position", "%"), ("target_temp", ""))


def _list_state(values: dict) -> str:
    """"on at 80%" / "off" / "locked" / "open (40%)" from one get_state `values` dict; "" when it has nothing to say."""
    power, lock = values.get("power"), values.get("lock")
    level = next((f"{fmt_value(values[c])}{u}" for c, u in _LIST_LEVELS if isinstance(values.get(c), (int, float))
                  and not isinstance(values.get(c), bool)), "")
    if isinstance(power, bool):
        return ("on" if power else "off") + (f" at {level}" if power and level else "")
    if isinstance(lock, str):
        return clean_label(lock, 12)
    return level


def describe_list(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """A `list_devices` outcome rendered for the user: at most `LIST_MAX` devices (name, room, and the current on/off and
    level when a `get_state` of the same turn supplies it). The hub's cleaned list result carries only a count, so names
    and rooms come from the (scoped) registry the caller passes, filtered by the call's own room/type arguments."""
    lists = [o for o in outs if o.ok and o.tool == "list_devices"]
    if not lists:
        return ""
    room, typ = lists[0].args.get("room"), lists[0].args.get("type")
    devs = sorted((d for d in reg.devices.values() if (not room or d.room == room) and (not typ or d.type == typ)),
                  key=lambda d: (d.room.lower(), d.name.lower(), d.endpoint_id))
    if not devs:
        return "I don't see any devices."
    live: dict[str, dict] = {}
    for o in outs:
        if o.ok and o.tool == "get_state":
            for s in o.result.get("states") or []:
                if isinstance(s, dict) and isinstance(s.get("endpoint_id"), str):
                    live[s["endpoint_id"]] = s
    items = []
    for d in devs[:LIST_MAX]:
        s = live.get(d.endpoint_id)
        st = "offline" if s is not None and not s.get("online") else (_list_state(s.get("values") or {}) if s else "")
        items.append(f"{d.name} ({d.room})" + (f" {st}" if st else ""))
    n, more = len(devs), len(devs) - LIST_MAX
    head = f"You have {n} device{'s' if n != 1 else ''}: "
    return head + "; ".join(items) + (f"; and {more} more." if more > 0 else ".")


_QUESTION_UNIT = {"brightness": "%", "position": "%"}
_QUESTION_LABEL = {"brightness": "brightness", "position": "position", "speed": "speed",
                   "target_temp": "temperature", "temperature": "temperature"}


def describe_question(outs: list[CallOutcome], reg: DeviceRegistry, question: tuple) -> str:
    """(S8l) The one-line answer to a `resolve_question` fast-path match (`sched/resolver.py`), composed from the
    verified `get_state` result(s): never a model draft, and never the multi-capability dump `describe_reads` gives
    for a plain "device status" question. `question` is `(mode, capability, expect)`:
    bool/lock/position_bool compare the live value against what the user asked ("is X on?"), "value" just states the
    current reading ("what is X's brightness?"), "any" (no device named, e.g. "is anything on?") scans every state
    this turn fetched. Empty string when the read failed or the capability is missing from the result (the caller
    falls back to its own default text)."""
    mode, cap, expect = question[:3]
    only = question[3] if len(question) > 3 else None          # "list" mode: the endpoint ids the question is about
    states = [s for o in outs if o.ok and o.tool == "get_state" for s in (o.result.get("states") or []) if isinstance(s, dict)]
    if not states:
        return ""
    if mode == "list":
        # "which devices are on?": only the devices that are (the listed ones; offline devices are not "on")
        verb = "on" if expect else "off"
        hits = [s for s in states if s.get("online") and s.get("values", {}).get(cap) is expect
                and (only is None or s.get("endpoint_id") in only)]
        if not hits:
            return f"Nothing is {verb}." if expect else "Everything is on."
        items = [f"{name(reg, s.get('endpoint_id'))} ({reg.devices[s['endpoint_id']].room})" if s.get("endpoint_id") in reg.devices
                 else name(reg, s.get("endpoint_id")) for s in hits]
        return f"{'On' if expect else 'Off'} right now: {join_names(items)}."
    if mode == "any":
        verb = "on" if expect else "off"
        hits = [s for s in states if s.get("online") and s.get("values", {}).get(cap) is expect]
        if not hits:
            return f"No, nothing is {verb}." if expect else "No, everything is off."
        shown = join_names([name(reg, s.get("endpoint_id")) for s in hits])
        plural = "is" if len(hits) == 1 else "are"
        return f"Yes, {shown} {plural} {verb}."
    s = states[0]
    nm = name(reg, s.get("endpoint_id"))
    if not s.get("online"):
        return f"{nm} is offline."
    vals = s.get("values") or {}
    actual = vals.get(cap)
    if mode == "bool":
        if not isinstance(actual, bool):
            return ""
        extra = f" at {vals['brightness']}%" if actual and cap == "power" and isinstance(vals.get("brightness"), (int, float)) else ""
        return f"{'Yes' if actual == expect else 'No'}, {nm} is {'on' if actual else 'off'}{extra}."
    if mode == "lock":
        if not isinstance(actual, str):
            return ""
        return f"{'Yes' if actual == expect else 'No'}, {nm} is {actual}."
    if mode == "position_bool":
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return ""
        is_open = actual > 0
        return f"{'Yes' if is_open == expect else 'No'}, {nm} is {'open' if is_open else 'closed'} ({int(actual)}%)."
    if mode == "value":
        if actual is None:
            return ""
        unit = _QUESTION_UNIT.get(cap, "")
        label = _QUESTION_LABEL.get(cap, cap)
        return f"{nm} {label} is {fmt_value(actual)}{unit}."
    return ""


def ambient_clarifying_text(domain: str, reg: DeviceRegistry) -> str:
    """(S8h) An ambient complaint that named no device and no number: ask, naming the real devices that could
    answer it rather than guessing one."""
    names = [d.name for d in ambient_domain_devices(reg, domain)]
    if not names:
        return "Which device do you mean?"
    if len(names) == 1:
        return f"Did you mean {names[0]}?"
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return f"Which one do you mean — {shown}?"
