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
