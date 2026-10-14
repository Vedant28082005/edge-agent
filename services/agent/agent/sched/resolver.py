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

Guests: the agent is told only `role`, not which rooms or scenes a guest was assigned (contract gap), so the fast path
never controls anything for a guest. `set_state` utterances are answered with `denied` and nothing is executed; scene
utterances fall through (the hub, not the agent, knows assignments)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..devices import Device, DeviceRegistry
from ..schema import validate_call
from .config import SchedConfig
from .text import (FILLER, NEGATION, PREP, QUANT, QUESTION, RELATIVE, TEMPORAL, TYPE_SYN, is_type_word, normalise,
                   score_device, stem, strip_polite)

PLAN, FALLTHROUGH, DENIED = "plan", "fallthrough", "denied"

_ON_PARTICLE_VERBS = {"turn", "switch", "power", "shut", "flip"}
_ON_VERBS = {"start", "enable", "activate", "resume"}
_OFF_VERBS = {"stop", "disable", "deactivate", "kill"}
_SET_VERBS = {"set", "change", "make", "dim", "adjust", "put", "turn", "brighten"}
_TIME_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:min|mins|minutes?|hours?|hrs?|seconds?|secs?|am|pm)\b|\bevery\s+\w+|\bo clock\b")
_VALUE_RES = (re.compile(r"\b(?:to|at|by)\s+(\d+(?:\.\d+)?)\s*(%|percent)?"),
              re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(%|percent)"),
              re.compile(r"\b(?:speed|brightness|position|level|setting)\s+(\d+(?:\.\d+)?)\s*(%|percent)?\s*$"))
_CAP_WORDS = {"brightness": "brightness", "bright": "brightness", "dim": "brightness", "speed": "speed",
              "position": "position", "level": None, "setting": None}
_DEFAULT_CAP = {"light": "brightness", "fan": "speed", "cover": "position"}
_SCENE_WORDS = {"scene", "mode", "routine", "preset"}
_SCENE_VERBS = {"run", "play", "start", "activate", "launch", "trigger", "set", "enable", "execute"}


@dataclass
class Resolution:
    status: str                                  # plan | fallthrough | denied
    calls: list = field(default_factory=list)
    say: str = ""
    confidence: float = 0.0
    reason: str = ""                             # why it fell through / what matched
    intent: str = ""
    candidates: list = field(default_factory=list)   # [(endpoint_id | scene name, score)] best first, at most 5

    @property
    def is_plan(self) -> bool:
        return self.status == PLAN


def _fall(reason: str, intent: str = "", candidates: list | None = None, conf: float = 0.0) -> Resolution:
    return Resolution(FALLTHROUGH, reason=reason, intent=intent, candidates=candidates or [], confidence=conf)


@dataclass
class _Cmd:
    kind: str                                    # power | lock | cover | value | scene
    value: object = None
    cap: str | None = None                       # explicit capability word, if any
    conf: float = 1.0
    rest: list = field(default_factory=list)     # target tokens left after removing intent words


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
            return _fall("compound")
        if not reg.devices and not scenes:
            return _fall("empty_registry")

        sc = self._scene(s, toks, scenes, role)
        if sc is not None:
            return sc
        cmd = self._parse(s, toks)
        if isinstance(cmd, str):
            return _fall(cmd)
        return self._device_plan(cmd, reg, role, text)

    # ------------------------------------------------------------------------------------------ intent
    def _parse(self, s: str, toks: list[str]) -> _Cmd | str:
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
            return _Cmd("value", value=value, cap=cap, conf=1.0, rest=self._clean(rest))
        if "half" in tset or tset & {"full", "max", "maximum", "min", "minimum", "lowest", "highest"}:
            return "symbolic_value"                                       # needs the device's range: leave to the LLM
        if tset & RELATIVE:
            return "relative"
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
        return "no_intent"

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
            return _fall("guest_scene_scope_unknown", "scene", [(c, conf) for c in cands])
        if len(cands) != 1:
            return _fall("scene_ambiguous" if cands else "scene_unknown", "scene", [(c, conf) for c in cands][:5])
        if conf < self.cfg.fast_threshold:
            return _fall("low_confidence", "scene", [(cands[0], conf)], conf)
        return Resolution(PLAN, [{"tool": "run_scene", "args": {"scene": cands[0]}}], f"Running {cands[0]}.", conf,
                          "scene_match", "scene", [(cands[0], conf)])

    @staticmethod
    def _names_device_word(target: list[str]) -> bool:
        return any(is_type_word(t) for t in target)

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
        for d in members:
            tool_args = capable(d)
            args = {"endpoint_id": d.endpoint_id, "capability": tool_args[0], "value": tool_args[1]}
            err, clean = validate_call(reg, "set_state", args)
            if err:
                return _fall(err, intent, cands, conf)
            calls.append({"tool": "set_state", "args": clean})
        return Resolution(PLAN, calls, self._say(cmd, members), conf, "device_match", intent, cands)

    def _call_for(self, cmd: _Cmd, d: Device) -> tuple[str, object] | None:
        """(capability, value) this command means on device `d`, or None if `d` cannot do it."""
        if cmd.kind == "power":
            return ("power", cmd.value) if d.cap("power") and d.cap("power").kind == "actuator" else None
        if cmd.kind == "lock":
            c = d.cap("lock")
            return ("lock", cmd.value) if c and cmd.value in c.values else None
        if cmd.kind == "cover":
            c = d.cap("position")
            return ("position", cmd.value) if c and d.type == "cover" else None
        if cmd.kind == "value":
            num, pct = cmd.value
            cap = cmd.cap or _DEFAULT_CAP.get(d.type)
            c = d.cap(cap) if cap else None
            if c is None or c.kind != "actuator" or c.type not in ("int",) or c.min is None or c.max is None:
                return None
            if pct and not (c.min == 0 and c.max == 100):
                return None                                           # "50%" only means something on a 0..100 scale
            if num != int(num):
                return None
            return (cap, int(num))
        return None

    @staticmethod
    def _say(cmd: _Cmd, devs: list[Device]) -> str:
        names = ", ".join(d.name for d in devs[:3]) + (f" and {len(devs) - 3} more" if len(devs) > 3 else "")
        if cmd.kind == "power":
            return f"Turning {'on' if cmd.value else 'off'} {names}."
        if cmd.kind == "lock":
            return f"{'Locking' if cmd.value == 'locked' else 'Unlocking'} {names}."
        if cmd.kind == "cover":
            return f"{'Opening' if cmd.value else 'Closing'} {names}."
        return f"Setting {names}."
