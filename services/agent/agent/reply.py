"""Reply policy (S14): who writes the words the user reads.

The agent always composes the VERIFIED facts of a turn from the executor's outcomes (`render.describe_outcomes` /
`describe_reads`). The model may add a short natural `say` (planner.Plan.say) and, for read-only questions, a second short
LLM call may phrase the read results (`natural_read_replies`). Neither is trusted: a model text is shown only when
`say_verdict` / `phrase_verdict` find no contradiction with what actually ran or was read -- no claiming an action that did
not run, no state claimed without a read, no number or on/off/locked/open polarity that differs from the results.
Anything else falls back to the agent's own text, so a wrong or over-eager model can only ever lose its prettier wording."""
from __future__ import annotations

import re

from .devices import DeviceRegistry
from .executor import CallOutcome
from .render import fmt_value, name
from .util import clean_label

NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
_TOK_RE = re.compile(r"[a-z']+")
# word groups of opposite states: (side A, side B)
_POLAR = (({"on", "lit"}, {"off"}),
          ({"locked", "locking"}, {"unlocked", "unlocking", "unlock"}),
          ({"open", "opened", "opening"}, {"closed", "close", "closing", "shut"}))
CLAIM_RE = re.compile(r"\b(turn(?:ed|ing)|switch(?:ed|ing)|lock(?:ed|ing)|unlock(?:ed|ing)|open(?:ed|ing)|clos(?:ed|ing)|"
                      r"dimm(?:ed|ing)|rais(?:ed|ing)|lower(?:ed|ing)|start(?:ed|ing)|stopp(?:ed|ing)|activat(?:ed|ing)|"
                      r"setting|set to|done|all set)\b", re.I)
STATE_CLAIM_RE = re.compile(r"\b(?:is|are|currently|now|still)\s+(?:on|off|locked|unlocked|open|closed|\d)", re.I)
FAILURE_RE = re.compile(r"\b(couldn't|could not|can't|cannot|unable|failed|sorry|didn't|not able)\b", re.I)
MAX_PHRASE_CHARS = 240


# --------------------------------------------------------------------------------------------- numbers / polarity
def numbers_in(text: str) -> list[float]:
    return [float(m) for m in NUM_RE.findall(text or "")]


def _walk_numbers(v: object, out: set[float]) -> None:
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        out.add(float(v))
    elif isinstance(v, dict):
        for x in v.values():
            _walk_numbers(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _walk_numbers(x, out)


def outcome_numbers(outs: list[CallOutcome]) -> set[float]:
    out: set[float] = set()
    for o in outs:
        _walk_numbers(o.args, out)
        _walk_numbers(o.result, out)
    return out


def registry_numbers(reg: DeviceRegistry) -> set[float]:
    """Counts the model may legitimately state from the prompt's device list: devices, per type, per room, rooms, scenes."""
    out = {float(len(reg.devices)), float(len(reg.rooms())), float(len(reg.scene_names()))}
    for key in ("type", "room"):
        counts: dict[str, int] = {}
        for d in reg.devices.values():
            counts[getattr(d, key)] = counts.get(getattr(d, key), 0) + 1
        out.update(float(c) for c in counts.values())
    return out


def numbers_ok(text: str, allowed: set[float]) -> bool:
    """Every number in `text` equals an allowed one (a fractional source may be shown rounded)."""
    for n in numbers_in(text):
        if any(abs(n - a) < 1e-9 or (a != int(a) and n == int(n) and abs(n - a) <= 0.5) for a in allowed):
            continue
        return False
    return True


def polarity_conflict(text: str, facts: str) -> bool:
    """`text` asserts one side of an on/off, locked/unlocked or open/closed pair while `facts` has only the other."""
    t, f = set(_TOK_RE.findall(text.lower())), set(_TOK_RE.findall(facts.lower()))
    for a, b in _POLAR:
        if (t & a and not t & b and f & b and not f & a) or (t & b and not t & a and f & a and not f & b):
            return True
    return False


# --------------------------------------------------------------------------------------------- the model's `say`
def say_verdict(say: str, outs: list[CallOutcome], reg: DeviceRegistry, agent_text: str, user_text: str,
                stripped: bool = False) -> str | None:
    """None when the model's `say` may be shown for this turn, else a short reason code (kept for the trace/tests)."""
    if not say:
        return "empty"
    if stripped:
        return "stripped_write"                    # a question turn: whatever the model said about its write is moot
    if any(not o.ok for o in outs):
        return "outcome_not_ok"                    # declined / blocked / failed / rejected: the agent says exactly that
    if any(o.is_read for o in outs):
        return "read_turn"                         # values reach the user from the results, not from a text written before them
    writes = [o for o in outs if o.tool in ("set_state", "run_scene")]
    if not writes:
        if CLAIM_RE.search(say):
            return "claims_unrun_action"
        if STATE_CLAIM_RE.search(say):
            return "claims_state_without_read"
        allowed = registry_numbers(reg) | set(numbers_in(user_text))
        return None if numbers_ok(say, allowed) else "numbers"
    if FAILURE_RE.search(say):
        return "says_failure_after_success"
    if polarity_conflict(say, agent_text):
        return "polarity"
    if not numbers_ok(say, outcome_numbers(outs) | set(numbers_in(agent_text))):
        return "numbers"
    return None


# --------------------------------------------------------------------------------------------- natural read replies
def read_facts(outs: list[CallOutcome], reg: DeviceRegistry) -> str:
    """Compact, model-readable facts from the OK read results only (names are admin labels; values are validated fields)."""
    lines: list[str] = []
    for o in outs:
        if not o.ok:
            continue
        if o.tool == "get_state":
            for s in o.result.get("states") or []:
                if not isinstance(s, dict):
                    continue
                nm = name(reg, s.get("endpoint_id"))
                if not s.get("online"):
                    lines.append(f"{nm}: offline")
                    continue
                vals = []
                for k, v in sorted((s.get("values") or {}).items()):
                    shown = ("on" if v else "off") if (k == "power" and isinstance(v, bool)) else fmt_value(v)
                    vals.append(f"{clean_label(k, 24)} {shown}")
                lines.append(f"{nm}: " + (", ".join(vals) or "no data"))
        elif o.tool == "read_sensor":
            r = o.result
            lines.append(f"{name(reg, o.args.get('endpoint_id'))} {clean_label(r.get('capability'), 24)}: "
                         f"{fmt_value(r.get('value'))}{clean_label(r.get('unit'), 8)}" + (" (last known)" if r.get("stale") else ""))
        elif o.tool == "list_devices":
            lines.extend(f"{d.name} ({d.room}, {d.type})" for d in list(reg.devices.values())[:20])
    return "\n".join(lines)


READ_SYSTEM = ("You are a smart-home assistant. Answer the user's question in one short, friendly sentence, using ONLY "
               "the facts given. Add no numbers or states that are not in the facts. No lists, no markdown.")


def read_messages(question: str, facts: str) -> list[dict]:
    return [{"role": "system", "content": READ_SYSTEM},
            {"role": "user", "content": f"Facts:\n{facts}\n\nQuestion: {question}"}]


def phrase_verdict(text: str, facts: str) -> str | None:
    """None when a phrased read answer is consistent with the read facts, else a reason code."""
    t = " ".join((text or "").split())
    if not t:
        return "empty"
    if len(t) > MAX_PHRASE_CHARS:
        return "too_long"
    if not numbers_ok(t, set(numbers_in(facts))):
        return "numbers"
    if polarity_conflict(t, facts):
        return "polarity"
    return None
