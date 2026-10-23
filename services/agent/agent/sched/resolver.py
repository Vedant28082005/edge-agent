"""Fast path: a deterministic resolver for plain device commands. No LLM, no I/O.

    utterance -> intent (verb + value) -> target tokens -> score every registry device -> plan or fall through

A plan is returned only when (1) the intent is a plain imperative (no question, negation, condition, time, compound or
relative phrasing), (2) the target tokens are explained by exactly one device (or one explicit group such as "all lights
in the bedroom"), with coverage * intent confidence >= threshold and a clear margin over the runner-up, and (3) the call
passes the same `validate_call` the executor applies. Anything else is a `fallthrough` with a reason and the LLM gets the
turn. An unexplained word ("kitchen" when there is no kitchen device) lowers coverage instead of being ignored, so the
resolver never silently picks a different device than the one named.

The result is a list of `{"tool", "args"}` calls, executed through `Executor.run_calls` exactly like an LLM plan, so
budgets, the confirmation policy, hub validation, verify/settle and the action log are shared, not re-implemented.

Scope: the caller passes the registry view of the request's `allowed_rooms` / `allowed_scenes` and the scene names of that
view, so an out-of-scope device or scene cannot be matched at all (the executor enforces the scope again regardless).
Guests (S8b rule): they can never set_state or run_scene, so `set_state` utterances are answered with `denied` and
nothing is executed; scene utterances fall through to the LLM path, where the executor denies the call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..devices import Device, DeviceRegistry
from ..schema import _is_off_like, ensure_power_dependency, validate_call
from .config import SchedConfig
from .text import (FILLER, NEGATION, PREP, QUANT, QUESTION, RELATIVE, TEMPORAL, TYPE_SYN, is_type_word, normalise,
                   score_device, stem, strip_polite)

PLAN, FALLTHROUGH, DENIED, RELATIVE_PENDING = "plan", "fallthrough", "denied", "relative_pending"

_ON_PARTICLE_VERBS = {"turn", "switch", "power", "shut", "flip"}
_ON_VERBS = {"start", "enable", "activate", "resume"}
_OFF_VERBS = {"stop", "disable", "deactivate", "kill"}
_SET_VERBS = {"set", "change", "make", "dim", "adjust", "put", "turn", "brighten"}
_TIME_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:min|mins|minutes?|hours?|hrs?|seconds?|secs?|am|pm)\b|\bevery\s+\w+|\bo clock\b")
_VALUE_RES = (re.compile(r"\b(?:to|at)\s+(\d+(?:\.\d+)?)\s*(%|percent)?"),   # "by N" is relative (_relative_cmd), not absolute
              re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(%|percent)"),
              re.compile(r"\b(?:speed|brightness|position|level|setting)\s+(\d+(?:\.\d+)?)\s*(%|percent)?\s*$"))
_CAP_WORDS = {"brightness": "brightness", "bright": "brightness", "dim": "brightness", "speed": "speed",
              "position": "position", "temperature": "target_temp", "temp": "target_temp", "setpoint": "target_temp",
              "level": None, "setting": None}
_DEFAULT_CAP = {"light": "brightness", "fan": "speed", "cover": "position"}
# Direction (+1 / -1) each relative word means; words in RELATIVE (text.py) without an entry here (e.g. "bit",
# "little", "slightly") are magnitude hints only and never decide the sign on their own.
_RELATIVE_DIR = {"brighter": 1, "dimmer": -1, "higher": 1, "lower": -1, "more": 1, "less": -1, "increase": 1,
                 "decrease": -1, "raise": 1, "warmer": 1, "cooler": -1, "faster": 1, "slower": -1, "louder": 1,
                 "quieter": -1, "up": 1, "down": -1}
_BY_AMOUNT_RE = re.compile(r"\bby\s+(\d+(?:\.\d+)?)\s*(%|percent)?")
_PRONOUN = {"it", "them", "this", "that"}
_SCENE_WORDS = {"scene", "mode", "routine", "preset"}
_SCENE_VERBS = {"run", "play", "start", "activate", "launch", "trigger", "set", "enable", "execute"}


@dataclass
class Resolution:
    status: str                                  # plan | fallthrough | denied | relative
    calls: list = field(default_factory=list)
    say: str = ""
    confidence: float = 0.0
    reason: str = ""                             # why it fell through / what matched
    intent: str = ""
    candidates: list = field(default_factory=list)   # [(endpoint_id | scene name, score)] best first, at most 5
    relative: tuple | None = None                # (endpoint_id, capability, direction, amount|None, pct) for RELATIVE:
                                                  # the current value is unknown here (no live state, no I/O in the
                                                  # resolver), so the scheduler fetches it and computes the target

    @property
    def is_plan(self) -> bool:
        return self.status == PLAN


def _fall(reason: str, intent: str = "", candidates: list | None = None, conf: float = 0.0) -> Resolution:
    return Resolution(FALLTHROUGH, reason=reason, intent=intent, candidates=candidates or [], confidence=conf)


@dataclass
class _Cmd:
    kind: str                                    # power | lock | cover | value | mode | relative | scene
    value: object = None
    cap: str | None = None                       # explicit capability word, if any
    conf: float = 1.0
    rest: list = field(default_factory=list)     # target tokens left after removing intent words
    on_verb: bool = False                        # (S8f) the utterance itself said "on" (vs. a bare "set/dim ... to N")


class FastPathResolver:
    def __init__(self, cfg: SchedConfig | None = None) -> None:
        self.cfg = cfg or SchedConfig()

    # ------------------------------------------------------------------------------------------ entry
    def resolve(self, text: str, reg: DeviceRegistry, role: str = "member", scenes: tuple[str, ...] = ()) -> Resolution:
        s = strip_polite(normalise(text))
        toks = s.split()
        if not toks:
            return _fall("empty")
        if toks[0] in QUESTION or text.strip().endswith("?") and toks[0] not in ("turn", "switch", "set", "lock", "unlock"):
            return _fall("question")
        if NEGATION & set(toks):
            return _fall("negation")
        if TEMPORAL & set(toks) or _TIME_RE.search(s):
            return _fall("temporal_or_conditional")
        if "and" in toks or "," in text and len(toks) > 3:
            sc = self._scene(s, toks, scenes, role)
            if sc is not None:
                return sc
            cp = self._compound_power_value(s, reg, role)
            if cp is not None:
                return cp
            return _fall("compound")
        if not reg.devices and not scenes:
            return _fall("empty_registry")

        sc = self._scene(s, toks, scenes, role)
        if sc is not None:
            return sc
        cmd = self._parse(s, toks, reg)
        if isinstance(cmd, str):
            return _fall(cmd)
        if cmd.kind == "relative":
            return self._relative_plan(cmd, reg, role)
        return self._device_plan(cmd, reg, role, text)

    # ------------------------------------------------------------------------------------------ intent
    def _parse(self, s: str, toks: list[str], reg: DeviceRegistry) -> _Cmd | str:
        tset = set(toks)
        # value commands: "set X to 40%", "dim X to 20", "fan speed 3"
        value: tuple[float, bool] | None = None
        rest_s = s
        for rx in _VALUE_RES:
            m = rx.search(s)
            if m:
                value = (float(m.group(1)), bool(m.group(2)) if m.lastindex and m.lastindex >= 2 else False)
                rest_s = (s[:m.start()] + " " + s[m.end():]).strip()
                break
        if value is not None:
            verbs = tset & (_SET_VERBS | _ON_PARTICLE_VERBS | {"open", "close"})
            if not verbs:
                return "no_verb"
            rest = [t for t in rest_s.split() if t not in _SET_VERBS | _ON_PARTICLE_VERBS | {"open", "close", "up", "down", "on", "off"}]
            cap = next((_CAP_WORDS[t] for t in rest if t in _CAP_WORDS and _CAP_WORDS[t]), None)
            rest = [t for t in rest if t not in _CAP_WORDS and t != "percent" and t != "%"]
            return _Cmd("value", value=value, cap=cap, conf=1.0, rest=self._clean(rest), on_verb="on" in tset)
        if "half" in tset or tset & {"full", "max", "maximum", "min", "minimum", "lowest", "highest"}:
            return "symbolic_value"                                       # needs the device's range: leave to the LLM
        if tset & RELATIVE:
            rel_cmd = self._relative_cmd(s, toks, tset)
            if rel_cmd is not None:
                return rel_cmd
            return "relative"                     # pronoun-only ("make it brighter") or an ambiguous direction
        # lock / unlock
        for v, val in (("unlock", "unlocked"), ("lock", "locked")):
            if v in tset:
                return _Cmd("lock", value=val, conf=1.0, rest=self._clean([t for t in toks if t != v]))
        # open / close covers
        for v, val in (("open", 100), ("close", 0)):
            if v in tset:
                return _Cmd("cover", value=val, conf=0.95, rest=self._clean([t for t in toks if t != v]))
        # power
        on, off = "on" in tset, "off" in tset
        if on and off:
            return "on_and_off"
        if (on or off) and tset & _ON_PARTICLE_VERBS:
            drop = _ON_PARTICLE_VERBS | {"on", "off"}
            return _Cmd("power", value=on, conf=1.0, rest=self._clean([t for t in toks if t not in drop]))
        for verbs, val in ((_ON_VERBS, True), (_OFF_VERBS, False)):
            if tset & verbs:
                return _Cmd("power", value=val, conf=0.95, rest=self._clean([t for t in toks if t not in verbs]))
        # generic enum ("mode") commands: "set the thermostat to cool", "switch the thermostat to eco mode" --
        # matched against the capability VALUES the live registry itself announced, never a fixed word list, so a
        # brand new enum value on a brand new device works with no agent change.
        mode_cmd = self._mode_cmd(toks, tset, reg)
        if mode_cmd is not None:
            return mode_cmd
        return "no_intent"

    @staticmethod
    def _relative_cmd(s: str, toks: list[str], tset: set) -> "_Cmd | None":
        """"raise the bedroom fan speed by 2", "turn up the bedroom fan" -> a `relative` command (direction, an
        optional named magnitude, and the target device tokens). The current value is read from the live registry
        by the scheduler, not here (the resolver does no I/O), so this only ever identifies intent + target; None
        when the direction is missing/ambiguous or nothing but a pronoun is left, so the caller falls through to
        the LLM exactly as before."""
        dirs = {_RELATIVE_DIR[t] for t in tset if t in _RELATIVE_DIR}
        if len(dirs) != 1:
            return None
        direction = next(iter(dirs))
        m = _BY_AMOUNT_RE.search(s)
        amount = float(m.group(1)) if m else None
        pct = bool(m and m.group(2))
        rest_s = (s[:m.start()] + " " + s[m.end():]) if m else s
        drop = RELATIVE | _SET_VERBS | _ON_PARTICLE_VERBS | {"by"} | _PRONOUN
        rest = [t for t in rest_s.split() if t not in drop and not t.replace(".", "", 1).isdigit()
                and t not in ("percent", "%")]
        cap = next((_CAP_WORDS[t] for t in rest if t in _CAP_WORDS and _CAP_WORDS[t]), None)
        rest = [t for t in rest if t not in _CAP_WORDS]
        if not rest:
            return None
        return _Cmd("relative", value=(direction, amount, pct), cap=cap, conf=1.0, rest=FastPathResolver._clean(rest))

    @staticmethod
    def _mode_cmd(toks: list[str], tset: set, reg: DeviceRegistry) -> "_Cmd | None":
        """"set the thermostat to cool" -> a `mode` (or other enum) capability command, matched against the VALUES
        the live registry itself announced for that capability, never a fixed word list. Needs an explicit set/
        switch verb (so "cool" alone never fires); the literal word "mode" is deliberately not a trigger here, since
        `_scene` (checked before `_parse` runs) already treats it as naming a scene ("morning mode")."""
        verbs = tset & (_SET_VERBS | {"switch"})
        if not verbs:
            return None
        enum_vals: dict[str, set[str]] = {}
        for d in reg.devices.values():
            for c in d.caps:
                if c.kind == "actuator" and c.type == "enum" and c.id != "lock" and c.values:
                    enum_vals.setdefault(c.id, set()).update(c.values)
        hit = next(((cap_id, v) for cap_id, vals in enum_vals.items() for v in vals if v in tset), None)
        if hit is None:
            return None
        cap_id, value = hit
        rest = [t for t in toks if t not in (value, "mode", "to") and t not in verbs]
        return _Cmd("mode", value=value, cap=cap_id, conf=1.0, rest=FastPathResolver._clean(rest))

    @staticmethod
    def _clean(tokens: list[str]) -> list[str]:
        return [stem(t) for t in tokens if t not in FILLER and t not in PREP and t not in QUANT and t != "up" and t != "down"]

    # ------------------------------------------------------------------------------------------ scenes
    def _scene(self, s: str, toks: list[str], scenes: tuple[str, ...], role: str) -> Resolution | None:
        """A scene is named only with an explicit scene word ("run movie scene") or when the whole utterance is a scene name
        ("good night"). Returns None when the text is not a scene request at all."""
        if not scenes:
            return None
        sset = set(toks)
        words = [t for t in toks if t not in FILLER and t not in PREP]
        explicit = bool(sset & _SCENE_WORDS)
        target = [stem(t) for t in words if t not in _SCENE_WORDS and t not in _SCENE_VERBS]
        norm_names = {n: [stem(t) for t in normalise(n).split()] for n in scenes}
        exact = [n for n, nt in norm_names.items() if nt == [stem(t) for t in words]]
        if not explicit and len(exact) == 1 and not (sset & _SCENE_VERBS):
            cands, conf = exact, 1.0                                      # bare "good night"
        elif explicit or (sset & _SCENE_VERBS and target and any(set(target) <= set(nt) for nt in norm_names.values())
                          and not self._names_device_word(target)):
            if not target:
                return _fall("scene_unnamed", "scene")
            cands = [n for n, nt in norm_names.items() if set(target) <= set(nt)]
            if not explicit and not cands:
                return None
            exactm = [n for n in cands if norm_names[n] == target]
            conf = 1.0 if exactm else 0.9
            cands = exactm or cands
        else:
            return None
        if role == "guest":
            return _fall("guest_scene_denied", "scene", [(c, conf) for c in cands])
        if len(cands) != 1:
            return _fall("scene_ambiguous" if cands else "scene_unknown", "scene", [(c, conf) for c in cands][:5])
        if conf < self.cfg.fast_threshold:
            return _fall("low_confidence", "scene", [(cands[0], conf)], conf)
        return Resolution(PLAN, [{"tool": "run_scene", "args": {"scene": cands[0]}}], f"Running {cands[0]}.", conf,
                          "scene_match", "scene", [(cands[0], conf)])

    @staticmethod
    def _names_device_word(target: list[str]) -> bool:
        return any(is_type_word(t) for t in target)

    # ------------------------------------------------------------------------------------------ relative (S8g)
    def _relative_plan(self, cmd: _Cmd, reg: DeviceRegistry, role: str) -> Resolution:
        """Resolve the device (and, failing an explicit capability word, its one numeric actuator capability) for a
        `relative` command. No I/O here: the actual target value needs the live state, so this only returns a
        `RELATIVE` resolution carrying what the scheduler needs to fetch it and compute the new value; the resolver
        itself never guesses or caches a current value."""
        xs = list(cmd.rest)
        if not xs:
            return _fall("no_target", "relative")
        scored = [(sc, d) for d in reg.devices.values() for sc, ident in (score_device(xs, d),) if sc > 0 and ident]
        scored.sort(key=lambda t: (-t[0], t[1].endpoint_id))
        cands = [(d.endpoint_id, round(sc, 3)) for sc, d in scored[:5]]
        if not scored:
            return _fall("no_match", "relative", cands)
        top = scored[0][0]
        if top < self.cfg.fast_threshold:
            return _fall("low_confidence", "relative", cands, top)
        winners = [d for sc, d in scored if sc >= top - 1e-9]
        runner = max((sc for sc, d in scored if sc < top - 1e-9), default=0.0)
        if len(winners) > 1 or top - runner < self.cfg.fast_margin:
            return _fall("ambiguous", "relative", cands, top)
        d = winners[0]
        cap_id = cmd.cap or self._auto_cap(d)
        c = d.cap(cap_id) if cap_id else None
        if c is None or c.kind != "actuator" or c.type not in ("int", "float") or c.min is None or c.max is None:
            return _fall("capability_missing", "relative", cands, top)
        if role == "guest":
            return Resolution(DENIED, reason="guest_cannot_control", intent="relative", candidates=cands, confidence=top)
        direction, amount, pct = cmd.value
        return Resolution(RELATIVE_PENDING, confidence=top, reason="relative_match", intent="relative", candidates=cands,
                          relative=(d.endpoint_id, cap_id, direction, amount, pct))

    # ------------------------------------------------------------------------------------------ devices
    def _device_plan(self, cmd: _Cmd, reg: DeviceRegistry, role: str, raw_text: str) -> Resolution:
        intent = cmd.kind
        plural_types = {t for t in re.findall(r"[a-z]+", normalise(raw_text)) if t.endswith("s") and is_type_word(stem(t))}
        group = bool(QUANT & set(normalise(raw_text).split())) or bool(plural_types)
        xs = list(cmd.rest)
        if not xs:
            return _fall("no_target", intent)
        scored = []
        for d in reg.devices.values():
            sc, ident = score_device(xs, d)
            if sc > 0 and ident:
                scored.append((sc, d))
        scored.sort(key=lambda t: (-t[0], t[1].endpoint_id))
        cands = [(d.endpoint_id, round(sc, 3)) for sc, d in scored[:5]]
        if not scored:
            return _fall("no_match", intent, cands)
        top = scored[0][0]

        def capable(d: Device) -> tuple[str, object] | None:
            return self._call_for(cmd, d)

        winners = [d for sc, d in scored if sc >= top - 1e-9]
        runner = max((sc for sc, d in scored if sc < top - 1e-9), default=0.0)
        conf = top * cmd.conf
        if conf < self.cfg.fast_threshold:
            return _fall("low_confidence", intent, cands, conf)
        if group:
            members = [d for d in winners if capable(d)]
            # winners that cannot do it (a sensor named like the lights) are skipped silently in a group
            if not members:
                return _fall("capability_missing", intent, cands, conf)
            if len(members) > self.cfg.fast_max_group:
                return _fall("group_too_large", intent, cands, conf)
        else:
            if len(winners) > 1:
                return _fall("ambiguous", intent, cands, conf)
            if top - runner < self.cfg.fast_margin:
                return _fall("ambiguous", intent, cands, conf)
            if not capable(winners[0]):
                return _fall("capability_missing", intent, cands, conf)
            members = winners
        if role == "guest":
            return Resolution(DENIED, reason="guest_cannot_control", intent=intent, candidates=cands, confidence=conf)
        calls = []
        value_cap: str | None = None
        for d in members:
            cap, val = capable(d)
            value_cap = cap if cmd.kind == "value" else value_cap
            args = {"endpoint_id": d.endpoint_id, "capability": cap, "value": val}
            err, clean = validate_call(reg, "set_state", args)
            if err:
                return _fall(err, intent, cands, conf)
            calls.append({"tool": "set_state", "args": clean})
        # (S8g) generic power-dependency rule, shared with the LLM path: never per-capability logic here.
        calls = ensure_power_dependency(calls, reg)
        turned_on_eps = {c["args"]["endpoint_id"] for c in calls
                         if c["args"].get("capability") == "power" and c["args"].get("value") is True}
        turned_on = [d for d in members if d.endpoint_id in turned_on_eps]
        return Resolution(PLAN, calls, self._say(cmd, members, turned_on, value_cap), conf, "device_match", intent, cands)

    @staticmethod
    def _auto_cap(d: Device) -> str | None:
        """The capability a bare value command means on `d` with no explicit capability word: the common per-type
        hint when the type has one (light->brightness, fan->speed, cover->position), else -- so a brand new device
        type works with no agent change -- the device's own single non-power numeric actuator capability, if there
        is exactly one (ambiguous when there is more than one, so `_call_for` returns None and the caller falls
        through to the LLM rather than guessing)."""
        hint = _DEFAULT_CAP.get(d.type)
        if hint:
            return hint
        numeric = [c.id for c in d.caps if c.kind == "actuator" and c.type in ("int", "float") and c.id != "power"]
        return numeric[0] if len(numeric) == 1 else None

    def _call_for(self, cmd: _Cmd, d: Device) -> tuple[str, object] | None:
        """(capability, value) this command means on device `d`, or None if `d` cannot do it. Driven entirely by
        `d`'s own announced capabilities (ids, kinds, ranges) -- never by `d.type` alone, except as a tie-break hint
        in `_auto_cap` for the common device shapes."""
        if cmd.kind == "power":
            return ("power", cmd.value) if d.cap("power") and d.cap("power").kind == "actuator" else None
        if cmd.kind == "lock":
            c = d.cap("lock")
            return ("lock", cmd.value) if c and cmd.value in c.values else None
        if cmd.kind == "cover":
            c = d.cap("position")
            return ("position", cmd.value) if c else None
        if cmd.kind == "value":
            num, pct = cmd.value
            cap = cmd.cap or self._auto_cap(d)
            c = d.cap(cap) if cap else None
            if c is None or c.kind != "actuator" or c.type not in ("int", "float") or c.min is None or c.max is None:
                return None
            if pct and not (c.min == 0 and c.max == 100):
                return None                                           # "50%" only means something on a 0..100 scale
            if c.type == "int":
                if num != int(num):
                    return None
                return (cap, int(num))
            return (cap, float(num))
        if cmd.kind == "mode":
            c = d.cap(cmd.cap) if cmd.cap else None
            return (cmd.cap, cmd.value) if c and c.kind == "actuator" and c.type == "enum" and cmd.value in c.values else None
        return None

    @staticmethod
    def _say(cmd: _Cmd, devs: list[Device], turned_on: list[Device] | None = None, value_cap: str | None = None) -> str:
        names = ", ".join(d.name for d in devs[:3]) + (f" and {len(devs) - 3} more" if len(devs) > 3 else "")
        if cmd.kind == "power":
            return f"Turning {'on' if cmd.value else 'off'} {names}."
        if cmd.kind == "lock":
            return f"{'Locking' if cmd.value == 'locked' else 'Unlocking'} {names}."
        if cmd.kind == "cover":
            return f"{'Opening' if cmd.value else 'Closing'} {names}."
        if cmd.kind == "value" and value_cap == "brightness":
            num = int(cmd.value[0])
            return f"{names} turned on at {num}%." if turned_on else f"{names} brightness set to {num}."
        return f"Setting {names}."

    # ------------------------------------------------------------------------------------------ compound (S8f/S8g)
    def _compound_power_value(self, s: str, reg: DeviceRegistry, role: str) -> Resolution | None:
        """"turn on X and set Y to N" (either order, "and" means one device, one turn), for any non-off-like
        actuator value -- brightness, speed, position, a mode name, ... (never hard-coded to one capability): run
        both calls without the LLM, power first. Anything that is not exactly this shape (device unresolved,
        ambiguous, or the two clauses are not a power-on + a value/mode command) returns None and the generic
        compound fallthrough applies."""
        if "," in s or s.count(" and ") != 1:
            return None
        halves = [h.split() for h in s.split(" and ", 1)]
        if not all(halves):
            return None
        cmds = [self._parse(" ".join(h), h, reg) for h in halves]
        if any(isinstance(c, str) for c in cmds):
            return None
        for power_cmd, value_cmd in ((cmds[0], cmds[1]), (cmds[1], cmds[0])):
            if power_cmd.kind != "power" or power_cmd.value is not True or value_cmd.kind not in ("value", "mode"):
                continue
            pronoun = {"it", "them", "this", "that"}
            xs = [t for t in value_cmd.rest if t not in pronoun] or [t for t in power_cmd.rest if t not in pronoun]
            if not xs:
                return None
            scored = sorted(((sc, d) for d in reg.devices.values() for sc, ident in (score_device(xs, d),) if sc > 0 and ident),
                            key=lambda t: (-t[0], t[1].endpoint_id))
            if not scored:
                return None
            top = scored[0][0]
            runner = max((sc for sc, d in scored if sc < top - 1e-9), default=0.0)
            if top < self.cfg.fast_threshold or top - runner < self.cfg.fast_margin:
                return None
            d = scored[0][1]
            tool_args = self._call_for(value_cmd, d)
            if tool_args is None or _is_off_like(tool_args[1]):
                return None
            if d.cap("power") is None or d.cap("power").kind != "actuator" or d.cap("power").type != "bool":
                return None
            if role == "guest":
                return Resolution(DENIED, reason="guest_cannot_control", intent="value", candidates=[(d.endpoint_id, top)], confidence=top)
            pow_args = {"endpoint_id": d.endpoint_id, "capability": "power", "value": True}
            val_args = {"endpoint_id": d.endpoint_id, "capability": tool_args[0], "value": tool_args[1]}
            perr, pclean = validate_call(reg, "set_state", pow_args)
            verr, vclean = validate_call(reg, "set_state", val_args)
            if perr or verr:
                return None
            calls = [{"tool": "set_state", "args": pclean}, {"tool": "set_state", "args": vclean}]
            shown = f"{tool_args[1]}%" if tool_args[0] == "brightness" else f"{tool_args[0]} {tool_args[1]}"
            return Resolution(PLAN, calls, f"{d.name} on, {shown}.", top, "compound_power_value", "value",
                              [(d.endpoint_id, top)])
        return None
