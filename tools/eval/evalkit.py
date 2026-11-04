"""S15 evaluation harness, the pure part (no network): turn the live device registry into roles, expand the phrase file into
concrete steps with concrete expectations, and score what the agent did.

A phrase file case looks like (see phrases.json):

    {"id": "cmd-fan-on", "cat": "command", "needs": ["fan"], "setup": [{"dev": "fan", "cap": "power", "value": true}],
     "steps": [{"say": "turn on the {fan}", "expect": {"writes": [{"dev": "fan", "cap": "power", "value": true}]}}]}

Placeholders ({fan}, {light_unique}, {room}, ...) are replaced with the REAL names from the registry (`resolve_roles`); a case whose
roles the registry cannot supply is skipped with the reason. `expect` keys:

    writes         every one must have been EXECUTED (set_state ok): {"dev"|"group", "cap", "value" | "lt" | "gt"}
                   "group" = every power-switchable, non-risk:confirm device of "room" | "other_room" | "all" | "type:<t>"
    no_action      nothing may have been executed (any executed write counts as a wrong-device write)
    reads          at least one read tool (get_state / read_sensor) must have succeeded
    reply_match    regex the reply must match;  reply_not: regexes it must not match
    confirm        "any" (default) | "expect" (a confirmation request must have been raised) | "forbid"

Scoring: correct = every expectation met and nothing else written. A write that is not on an expected device is `wrong_device`
(for a no_action case every executed write is). An extra `power: true` on an expected device is the generic power-dependency rule
and is allowed; any other extra write is a failure."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

READ_TOOLS = ("get_state", "read_sensor", "list_devices")
WRITE_TOOLS = ("set_state", "run_scene")


# --------------------------------------------------------------------------------------------- registry -> roles
def _caps(d: dict) -> dict:
    return {c.get("id"): c for c in d.get("capabilities") or [] if isinstance(c, dict)}


def _switchable(d: dict) -> bool:
    c = _caps(d).get("power")
    return bool(c and c.get("kind", "actuator") == "actuator" and d.get("risk") != "confirm")


def _tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.lower()))


@dataclass
class Roles:
    devices: dict[str, dict]                       # endpoint_id -> device (enabled and online only)
    refs: dict[str, str] = field(default_factory=dict)      # role -> endpoint_id
    vars: dict[str, str] = field(default_factory=dict)      # placeholder -> text for a phrase

    def name(self, ep: str) -> str:
        d = self.devices.get(ep)
        return d["name"] if d else ep

    def group(self, spec: str) -> list[str]:
        room = self.vars.get("room") if spec == "room" else self.vars.get("other_room") if spec == "other_room" else None
        out = []
        for ep, d in sorted(self.devices.items(), key=lambda t: (t[1].get("room", ""), t[1].get("name", ""))):
            if not _switchable(d):
                continue
            if spec in ("room", "other_room") and d.get("room") != room:
                continue
            if spec.startswith("type:") and d.get("type") != spec[5:]:
                continue
            out.append(ep)
        return out

    def others_regex(self, keep: set[str]) -> str:
        """A regex of the names of the power devices NOT in `keep` (a listing of what is on must not mention them)."""
        keep_names = " ".join(self.name(e).lower() for e in keep)
        names = [self.name(e) for e, d in self.devices.items() if _switchable(d) and e not in keep and self.name(e).lower() not in keep_names]
        return r"\b(" + "|".join(re.escape(n) for n in names) + r")\b" if names else r"(?!x)x"


def resolve_roles(registry: list[dict]) -> Roles:
    devs = {d["endpoint_id"]: d for d in registry if d.get("enabled", True) and d.get("online", True)}
    r = Roles(devs)
    by_type: dict[str, list[dict]] = {}
    for d in sorted(devs.values(), key=lambda d: (d.get("room", ""), d.get("name", ""))):
        by_type.setdefault(d.get("type", ""), []).append(d)

    def put(role: str, d: dict, var: str | None = None) -> None:
        r.refs[role] = d["endpoint_id"]
        r.vars[var or role] = d["name"]

    lights = [d for d in by_type.get("light", []) if _switchable(d)]
    if lights:
        tokens = [_tokens(d["name"]) for d in lights]
        distinct = [(len(t), d) for d, t in zip(lights, tokens) if all(t - o for o in tokens if o is not t)]
        unique = max(distinct, key=lambda x: x[0])[1] if distinct else lights[0]
        put("light_unique", unique)
        if len(lights) > 1:
            r.vars["lights_ambiguous"] = "yes"
            r.refs["light_pair"] = lights[0]["endpoint_id"]
            r.vars["light_pair"] = " / ".join(d["name"] for d in lights[:2])
    fans = [d for d in by_type.get("fan", []) if _switchable(d)]
    if fans:
        fan = fans[0]
        put("fan", fan)
        sp = _caps(fan).get("speed")
        if sp and sp.get("min") is not None and sp.get("max") is not None and sp["max"] > sp["min"]:
            lo, hi = int(sp["min"]), int(sp["max"])
            r.vars["fan_speed"] = str(min(hi, max(lo, 3)))
            r.vars["fan_speed_hi"] = str(hi)
            r.vars["fan_speed_lo"] = str(lo)
    covers = [d for d in by_type.get("cover", []) if "position" in _caps(d) and d.get("risk") != "confirm"]
    if len(covers) == 1:                                       # "the curtain" must mean one thing
        put("cover", covers[0])
    outlets = [d for d in by_type.get("outlet", []) if _switchable(d)]
    if outlets:
        put("outlet", outlets[0])
    locks = [d for d in devs.values() if d.get("risk") == "confirm"]
    if locks:
        put("lock", locks[0])
    for d in devs.values():
        if "temperature" in _caps(d) and _caps(d)["temperature"].get("kind") == "sensor":
            put("temp_sensor", d)
            break
    rooms = sorted({d.get("room", "") for d in devs.values() if _switchable(d)})
    anchor = (fans or lights or outlets or [None])[0]
    if anchor:
        r.vars["room"] = anchor["room"]
        r.refs["room"] = anchor["endpoint_id"]
        others = [x for x in rooms if x != anchor["room"]]
        if others:
            r.vars["other_room"] = others[0]
            r.refs["other_room"] = others[0]
    r.vars.setdefault("bogus", "kitchen oven")
    existing = {d.get("room", "").lower() for d in devs.values()}
    r.vars["bogus_room"] = next(x for x in ("garage", "attic", "cellar", "gazebo") if x not in existing)
    return r


# --------------------------------------------------------------------------------------------- phrase file -> steps
def _sub(text: str, vars: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: vars.get(m.group(1), m.group(0)), text)


def _value(v, vars: dict):
    if isinstance(v, str):
        s = _sub(v, vars)
        try:
            return int(s)
        except ValueError:
            return s
    return v


@dataclass
class Step:
    case: str
    index: int
    cat: str
    say: str
    role: str                    # member | guest
    expect: dict
    risk_confirm: bool = False


@dataclass
class Case:
    id: str
    cat: str
    role: str
    setup: list
    steps: list[Step]
    skip: str = ""


def _writes(spec: list, roles: Roles) -> list[dict]:
    out = []
    for w in spec:
        value = {k: _value(w[k], roles.vars) for k in ("value", "lt", "gt") if k in w}
        if "group" in w:
            for ep in roles.group(w["group"]):
                out.append({"ep": ep, "cap": w["cap"], **value})
        else:
            out.append({"ep": roles.refs[w["dev"]], "cap": w["cap"], **value})
    return out


def expand(raw_cases: list[dict], roles: Roles, have_guest: bool = True) -> list[Case]:
    cases = []
    for c in raw_cases:
        role = c.get("role", "member")
        case = Case(c["id"], c["cat"], role, [], [])
        missing = [n for n in c.get("needs", []) if n not in roles.refs and n not in roles.vars]
        if missing:
            case.skip = "registry has no " + ", ".join(missing)
        elif role == "guest" and not have_guest:
            case.skip = "no guest login given (--guest-user)"
        else:
            try:
                case.setup = _writes(c.get("setup", []), roles)
                ov = {**roles.vars, "others_on": roles.others_regex({w["ep"] for w in _writes(c.get("on_list", []), roles)})}
                for i, st in enumerate(c["steps"], 1):
                    e = st.get("expect", {})
                    exp = {"writes": _writes(e.get("writes", []), roles), "no_action": bool(e.get("no_action")),
                           "reads": bool(e.get("reads")), "confirm": e.get("confirm", "any"),
                           "reply_match": _sub(e["reply_match"], ov) if e.get("reply_match") else None,
                           "reply_not": [_sub(x, ov) for x in e.get("reply_not", [])]}
                    case.steps.append(Step(c["id"], i, c["cat"], _sub(st["say"], roles.vars), role, exp, bool(c.get("risk_confirm"))))
            except KeyError as ex:
                case.skip = f"registry has no {ex}"
        cases.append(case)
    return cases


# --------------------------------------------------------------------------------------------- scoring
@dataclass
class Outcome:
    """What one chat step produced, as read off the SSE stream."""
    tools: list[dict] = field(default_factory=list)      # {"tool", "args", "ok"} in call order (ok None = no result seen)
    reply: str = ""
    route: str = ""
    server_ms: int = 0
    wall_ms: int = 0
    confirmation_requested: bool = False
    error: str = ""
    req_id: str = ""


def executed_writes(out: Outcome) -> list[dict]:
    return [t for t in out.tools if t["tool"] in WRITE_TOOLS and t.get("ok")]


def tools_string(out: Outcome, roles: Roles) -> str:
    parts = []
    for t in out.tools:
        a = t.get("args") or {}
        ep = a.get("endpoint_id")
        who = roles.name(ep) if ep else (a.get("room") or a.get("scene") or "")
        detail = f".{a['capability']}={a['value']}" if "capability" in a and "value" in a else (f".{a['capability']}" if "capability" in a else "")
        mark = "" if t.get("ok") in (True, None) else "!"
        parts.append(f"{t['tool']}{mark}:{who}{detail}")
    return "; ".join(parts)


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _meets(w: dict, got: dict) -> bool:
    v = got.get("value")
    if "value" in w:
        return v == w["value"] if not (_num(v) is not None and _num(w["value"]) is not None) else _num(v) == _num(w["value"])
    if "lt" in w:
        return _num(v) is not None and _num(v) < _num(w["lt"])
    if "gt" in w:
        return _num(v) is not None and _num(v) > _num(w["gt"])
    return True


def score(exp: dict, out: Outcome, roles: Roles) -> tuple[bool, bool, list[str]]:
    """(correct, wrong_device, notes)."""
    notes: list[str] = []
    done = [{"ep": (t.get("args") or {}).get("endpoint_id"), "cap": (t.get("args") or {}).get("capability"),
             "value": (t.get("args") or {}).get("value"), "tool": t["tool"]} for t in executed_writes(out)]
    want_eps = {w["ep"] for w in exp["writes"]}
    wrong, extra = [], []
    for d in done:
        if d["tool"] != "set_state" or d["ep"] not in want_eps:
            wrong.append(d)
        elif not any(w["ep"] == d["ep"] and w["cap"] == d["cap"] and _meets(w, d) for w in exp["writes"]) and not (d["cap"] == "power" and d["value"] is True):
            extra.append(d)
    for w in exp["writes"]:
        if not any(d["ep"] == w["ep"] and d["cap"] == w["cap"] and d["tool"] == "set_state" and _meets(w, d) for d in done):
            notes.append(f"missing {roles.name(w['ep'])}.{w['cap']}")
    if wrong:
        notes.append("wrote " + ", ".join(f"{roles.name(d['ep']) if d['ep'] else d['tool']}" for d in wrong))
    if extra:
        notes.append("extra " + ", ".join(f"{roles.name(d['ep'])}.{d['cap']}={d['value']}" for d in extra))
    if exp["no_action"] and done and not wrong:
        notes.append("acted on a no-action phrase")
    if exp["reads"] and not any(t["tool"] in ("get_state", "read_sensor") and t.get("ok") for t in out.tools):
        notes.append("no successful read")
    if exp["confirm"] == "expect" and not out.confirmation_requested:
        notes.append("no confirmation request")
    if exp["confirm"] == "forbid" and out.confirmation_requested:
        notes.append("confirmation requested")
    if exp["reply_match"] and not re.search(exp["reply_match"], out.reply, re.I):
        notes.append("reply does not match")
    for rx in exp["reply_not"]:
        if re.search(rx, out.reply, re.I):
            notes.append("reply matches forbidden pattern")
    if out.error:
        notes.append("error " + out.error)
    if not out.reply and not out.error:
        notes.append("empty reply")
    wrong_device = bool(wrong)
    return not notes, wrong_device, notes


# --------------------------------------------------------------------------------------------- csv
COLUMNS = ["config", "id", "step", "category", "role", "phrase", "route", "decision", "tools_called", "correct", "wrong_device",
           "latency_ms", "server_latency_ms", "prompt_eval_tokens", "cache_hit_tokens", "llm_calls", "reply", "notes", "config_flags",
           "req_id"]


def merge_trace(row: dict, trace: dict | None) -> dict:
    """Add the server's own numbers for this request (matched by req_id) to a result row."""
    if not trace:
        return row
    row = dict(row)
    row["decision"] = trace.get("decision") or ""
    row["prompt_eval_tokens"] = trace.get("prompt_eval_tokens", trace.get("prompt_tokens", ""))
    row["cache_hit_tokens"] = trace.get("cache_hit_tokens", trace.get("cached_tokens", ""))
    row["llm_calls"] = trace.get("llm_calls", "")
    row["config_flags"] = json.dumps(trace.get("s15") or {}, sort_keys=True, separators=(",", ":")) if trace.get("s15") else ""
    if not row.get("route"):
        row["route"] = trace.get("route") or ""
    return row


EXPECTED_FLAGS = {
    "baseline": {"deterministic_fixes": False, "generated_prompt": False, "chat_route": False, "compact_json": False},
    "new": {"deterministic_fixes": True, "generated_prompt": True, "chat_route": True, "compact_json": False},
    "compact": {"deterministic_fixes": True, "generated_prompt": True, "chat_route": True, "compact_json": True},
}


def flag_warning(label: str, rows: list[dict]) -> str:
    """A sentence when the flags the server reports in its traces are not what the label says (agent.json not flipped / not restarted)."""
    want = EXPECTED_FLAGS.get(label)
    seen = {r["config_flags"] for r in rows if r.get("config_flags")}
    if not want or not seen:
        return ""
    bad = [s for s in seen if json.loads(s) != want]
    if bad:
        return f"WARNING: label '{label}' expects {json.dumps(want)} but the traces show {', '.join(sorted(seen))} - is agent.json flipped and the agent restarted?"
    return ""


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("correct") in ("yes", "no")]
    llm = [r for r in scored if r.get("llm_calls") not in ("", None, 0, "0")]

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    ev = [num(r["prompt_eval_tokens"]) for r in llm if num(r.get("prompt_eval_tokens")) is not None]
    lat = [num(r["latency_ms"]) for r in scored if num(r.get("latency_ms")) is not None]
    return {"phrases": len(scored), "correct": sum(r["correct"] == "yes" for r in scored),
            "wrong_device": sum(r["wrong_device"] == "yes" for r in scored),
            "mean_latency_ms": round(sum(lat) / len(lat)) if lat else None,
            "llm_turns": len(llm), "mean_eval_tokens_per_llm_turn": round(sum(ev) / len(ev), 1) if ev else None,
            "mean_cache_hit_tokens_per_llm_turn": round(sum(x for x in (num(r.get("cache_hit_tokens")) for r in llm) if x is not None) / max(1, len(llm)), 1)}
