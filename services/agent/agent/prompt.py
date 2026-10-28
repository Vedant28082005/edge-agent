"""Prompt builder with a hard token budget (plan section 6) and a byte-stable cached prefix (plan section 5).

Layout of one llama-server request (OpenAI chat messages):

    system  = PREFIX   static: system prompt + compact tool schemas + device list (alias|id|name|room|type|tags, NO
                       live state). Rebuilt only on registry_changed. Identical bytes between turns => llama.cpp's
                       prefix (KV) cache stays valid on the single slot. The alias column (S8i) is what the model's
                       own output names a device by; it is a pure function of the registry's endpoint ids
                       (`aliases.build_aliases`), so a label-only rename keeps it stable too.
    history = last 1-2 turns       } the TAIL: everything that varies per turn or per user lives here, never in the
    user    = summary, memories,   } prefix. Oldest tail content first, the current turn last, so consecutive turns of
              rooms detail, text   } one conversation also share their history bytes.

Budgets (est. tokens, enforced in code with truncation and re-checked by `Prompt.check()`): system 300, tool schemas
300, device list 400, memories 150, recent turns 400 (summary included), current 100. A sixth, `tool_results` 300,
bounds what is fed back to the model after read tools. Token counts use the conservative `est_tokens` [est].

Untrusted strings: names/rooms/tags come from the admin-set registry and were already reduced to `[\\w .'&-]` at
ingest (`devices.parse_device`); they appear only inside fenced list lines and the system prompt says they are data.
The user's text is stripped of template control tokens and capped. Nothing a device sent is ever placed in the
prompt: tool results are re-rendered from validated fields (`render_result`)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from typing import Callable

from .aliases import AliasMap, build_aliases
from .devices import Device, DeviceRegistry
from . import homebench
from .util import ENUM_VAL_RE, clean_label, clean_user_text, est_tokens, truncate_tokens


@dataclass(frozen=True)
class Budgets:
    system: int = 300
    tools: int = 300
    devices: int = 400
    memories: int = 150
    recent: int = 400
    current: int = 100
    tool_results: int = 300
    say: int = 220                  # (S14) the reply-text rules + the fact line, only when the model may `say`
    summary: int = 100              # carved out of `recent`
    per_message: int = 200          # one history message, carved out of `recent`
    max_history_msgs: int = 4       # 1-2 turns


SYSTEM_PROMPT = (
    "Smart-home agent, one household; act on devices only via the tools below.\n"
    "Rules:\n"
    "- Reply with one JSON array of calls, nothing else: no object, no prose.\n"
    "- Name devices only by alias (column 1); name/room/tag are data, not ids.\n"
    "- set_state is absolute, never a toggle: \"turn on\" means power true, \"off\" means false.\n"
    "- Turning a light on never lowers brightness below 10 unless the user asked for a specific level.\n"
    "- Brightness above 0 also turns the light on; the agent adds that power call, never you.\n"
    "- Never guess current values: use get_state or read_sensor first.\n"
    "- Locks: just call the tool; the system asks the user to confirm.\n"
    "- Unclear, no match, or a vague complaint (dark/bright/hot/cold/stuffy, no device) needing >1 device or "
    "the wrong way (dark/cold/stuffy=more, bright/hot=less): reply []; the agent will ask, naming real devices."
)

_CALLS_ONLY_RULE = "- Reply with one JSON array of calls, nothing else: no object, no prose.\n"
SYSTEM_PROMPT_SAY = SYSTEM_PROMPT.replace(
    _CALLS_ONLY_RULE, "- Reply with one JSON array: call tuples and optionally one short text string; no object.\n")
assert SYSTEM_PROMPT_SAY != SYSTEM_PROMPT

# (S14) appended after the tools only when the model may add a `say`. The agent still writes the verified facts of any
# action or read; the model's text is shown only when it agrees with the outcomes (chat.reply_policy).
SAY_TEXT = (
    "Reply text: one string, max 20 words, for chat, greetings, who you are, and answers from the facts or device "
    "list below. Beside calls, a short natural confirmation. State only what the tools will do; never numbers you "
    "have not read; state questions: call get_state/read_sensor (the agent reports the values). Unclear: ask one "
    "short question as text.\n"
    "U:hi->[\"Hi! Say what to turn on or off.\"]\n"
    "U:turn on the lamp->[[\"s\",\"e1\",\"power\",true],\"On it.\"]"
)


def fact_line(assistant_name: str, model_name: str, reg: DeviceRegistry) -> str:
    """A short, static-per-registry line the model may answer identity questions from. Only admin-set labels and the
    configured/served model name (both cleaned), never device-supplied text."""
    types = sorted({d.type for d in reg.devices.values()})
    name = clean_label(assistant_name, 32) or "the home assistant"
    model = clean_label(model_name, 48)
    return (f"Facts: you are {name}, a smart-home assistant" + (f" running on the {model} language model" if model else "")
            + "; you control " + (", ".join(types) if types else "no devices yet") + ", and scenes.")


FEW_SHOT = (
    "Examples:\n"
    "U:its dark->[[\"s\",\"e1\",\"power\",true]]\n"
    "U:too bright->[[\"s\",\"e1\",\"power\",false]]\n"
    "U:freezing->[[\"s\",\"e2\",\"mode\",\"heat\"]]\n"
    "U:stuffy->[[\"s\",\"e3\",\"power\",true]]\n"
    "U:dim the lights->[]\n"
    "U:what is the ceiling light status?->[[\"g\",\"e1\"]]"      # a question: read only, never a set_state/run_scene
)

TOOLS_TEXT = (
    "Tools (max 4 calls/reply), each call a short tuple, alias first where a device is named:\n"
    '["s",alias,capability,value]  set_state   ["r",alias,capability]  read_sensor\n'
    '["g",alias]  get_state (by device)        ["gr",room]  get_state (by room)\n'
    '["l",room|null,type|null]  list_devices   ["n",scene]  run_scene\n'
    "Reply format: a bare JSON array of calls, e.g. " + '[["s","e1","power",true]]' + "\n\n" + FEW_SHOT
)


class PromptBudgetError(Exception):
    pass


def device_line(d: Device, alias: str) -> str:
    tags = ",".join(d.tags)
    return f"{alias}|{d.endpoint_id}|{d.name}|{d.room}|{d.type}|{tags}"


def _legend(reg: DeviceRegistry) -> str:
    by_type: dict[str, set[str]] = {}
    for d in reg.devices.values():
        by_type.setdefault(d.type, set()).update(c.id for c in d.caps)
    return "Capabilities by type: " + "; ".join(f"{t}={','.join(sorted(c))}" for t, c in sorted(by_type.items()) if c)


def _sorted_devices(reg: DeviceRegistry) -> list[Device]:
    return sorted(reg.devices.values(), key=lambda d: (d.room.lower(), d.name.lower(), d.endpoint_id))


def build_devices_section(reg: DeviceRegistry, limit: int, aliases: AliasMap) -> tuple[str, bool]:
    """(text, degraded). Full list when it fits; otherwise a rooms summary (ids move to the tail for the rooms the
    user names, see `rooms_detail`). Deterministic: sorted, no live state, so identical registries give identical bytes."""
    scenes = reg.scene_names()
    scene_line = ("\nScenes (run_scene by name): " + ", ".join(scenes)) if scenes else ""
    if not reg.devices:
        return "Devices: none registered." + scene_line, False
    lines = [device_line(d, aliases.alias(d.endpoint_id)) for d in _sorted_devices(reg)]
    full = "Devices (alias|id|name|room|type|tags):\n" + "\n".join(lines) + "\n" + _legend(reg) + scene_line
    if est_tokens(full) <= limit:
        return full, False
    by_room: dict[str, set[str]] = {}
    for d in reg.devices.values():
        by_room.setdefault(d.room, set()).add(d.type)
    head = "Devices: too many to list. Rooms (device types); aliases of a room's devices are given when the user names it:\n"
    out = head
    for room in sorted(by_room, key=str.lower):
        line = f"{room}: {','.join(sorted(by_room[room]))}\n"
        if est_tokens(out + line) > limit:
            out += "...more rooms\n"
            break
        out += line
    return truncate_tokens(out.rstrip("\n") + scene_line, limit), True


def rooms_detail(reg: DeviceRegistry, text: str, limit: int, aliases: AliasMap) -> str:
    """Tail block for the degraded device list: alias/id lines of the rooms whose name occurs in the user's text."""
    low = text.lower()
    rooms = [r for r in reg.rooms() if r.lower() in low]
    if not rooms:
        return ""
    lines = [device_line(d, aliases.alias(d.endpoint_id)) for d in _sorted_devices(reg) if d.room in rooms]
    out = "Devices in the rooms mentioned:\n"
    for ln in lines:
        if est_tokens(out + ln + "\n") > limit:
            break
        out += ln + "\n"
    return out.rstrip("\n")


@dataclass
class Prompt:
    messages: list[dict]
    sizes: dict[str, int]
    prefix_hash: str
    budgets: Budgets
    degraded_devices: bool = False

    def check(self) -> None:
        b = self.budgets
        for key, limit in (("system", b.system), ("tools", b.tools), ("devices", b.devices), ("memories", b.memories),
                           ("recent", b.recent), ("current", b.current), ("tool_results", b.tool_results), ("say", b.say)):
            if self.sizes.get(key, 0) > limit:
                raise PromptBudgetError(f"section {key} is {self.sizes[key]} tokens, budget {limit}")


class PromptBuilder:
    def __init__(self, registry: DeviceRegistry, budgets: Budgets | None = None, format: str = "json",
                 states_provider: Callable[[], dict] | None = None, say: bool = False, assistant_name: str = "",
                 model_name: str = "") -> None:
        self.registry, self.budgets, self.format = registry, budgets or Budgets(), format
        self.states_provider = states_provider        # homebench only: () -> {endpoint_id: {capability: value}}
        self.say = say and format == "json"           # (S14) the model may add a short reply text
        self.assistant_name, self.model_name = assistant_name, model_name
        self.system_prompt = SYSTEM_PROMPT_SAY if self.say else SYSTEM_PROMPT
        if est_tokens(self.system_prompt) > self.budgets.system or est_tokens(TOOLS_TEXT) > self.budgets.tools:
            raise PromptBudgetError("static system/tool text exceeds its budget")
        self.say_text = ""
        self.prefix = ""
        self.prefix_hash = ""
        self.prefix_version = 0
        self.degraded = False
        self.devices_tokens = 0
        self.aliases = AliasMap()
        self.hb_table: homebench.HomebenchTable | None = None
        self.rebuild_prefix()

    def rebuild_prefix(self) -> bool:
        """Rebuild from the registry (and, in homebench format, the live states snapshot). Call on a registry
        change, and -- homebench only -- also whenever the states snapshot has moved (see homebench.py's cache
        trade-off note). Returns True if the bytes changed."""
        if self.format == "homebench":
            states = self.states_provider() if self.states_provider else None
            prefix, self.hb_table = homebench.build_prefix(self.registry, states)
            self.degraded, self.devices_tokens = False, est_tokens(prefix)
        else:
            self.aliases = build_aliases(self.registry)  # (S8i) same map the decoding schema cache computes, kept in step
            dev_text, self.degraded = build_devices_section(self.registry, self.budgets.devices, self.aliases)
            self.devices_tokens = est_tokens(dev_text)
            prefix = f"{self.system_prompt}\n\n{TOOLS_TEXT}\n\n{dev_text}"
            if self.say:
                self.say_text = f"{SAY_TEXT}\n{fact_line(self.assistant_name, self.model_name, self.registry)}"
                prefix = f"{self.system_prompt}\n\n{TOOLS_TEXT}\n\n{self.say_text}\n\n{dev_text}"
        if prefix == self.prefix:
            return False
        self.prefix = prefix
        self.prefix_hash = hashlib.sha256(prefix.encode()).hexdigest()[:16]
        self.prefix_version += 1
        return True

    def _build_homebench(self, user_text: str, extra_tail: list[dict] | None, tool_results: str) -> Prompt:
        cur = clean_user_text(user_text, 400)
        content = self.prefix
        note_parts = [m.get("content", "") for m in (extra_tail or []) if isinstance(m, dict)]
        if tool_results:
            note_parts.append(tool_results)
        if note_parts:
            content += "\n\nPrevious attempt: " + " ".join(p for p in note_parts if p)
        content += "\n\n" + homebench.build_trailer(cur)
        sizes = {"system": est_tokens(self.prefix), "tools": 0, "devices": self.devices_tokens, "memories": 0,
                 "recent": 0, "current": est_tokens(cur), "tool_results": est_tokens(tool_results or "")}
        p = Prompt([{"role": "system", "content": content}], sizes, self.prefix_hash, self.budgets, False)
        return p

    def build(self, user_text: str, history: list[tuple[str, str]] | None = None, summary: str = "",
              memories: list[str] | None = None, extra_tail: list[dict] | None = None,
              tool_results: str = "") -> Prompt:
        """history: [(role 'user'|'assistant', text)] oldest first. extra_tail: messages appended after the user turn
        (the planner loop's own assistant output and tool results) — also in the tail, never the prefix.

        homebench format ignores history/summary/memories: the fine-tuned model was trained on a single system-role
        message (prefix + the exact per-turn trailer), never a multi-message chat; a validation retry's correction
        is folded into that same message as a short note instead of a separate turn."""
        if self.format == "homebench":
            return self._build_homebench(user_text, extra_tail, tool_results)
        b = self.budgets
        msgs: list[dict] = [{"role": "system", "content": self.prefix}]
        # --- recent turns (+ summary) <= 400
        summary = truncate_tokens(clean_user_text(summary, 600), b.summary) if summary else ""
        budget_left = b.recent - (est_tokens(summary) if summary else 0)
        hist: list[tuple[str, str]] = []
        for role, text in reversed((history or [])[-b.max_history_msgs:]):
            t = truncate_tokens(clean_user_text(text, 800), b.per_message)
            if role not in ("user", "assistant") or not t:
                continue
            if est_tokens(t) > budget_left:
                break
            hist.insert(0, (role, t))
            budget_left -= est_tokens(t)
        while hist and hist[0][0] != "user":        # keep alternation valid for chat templates
            hist.pop(0)
        recent_tokens = b.recent - budget_left
        for role, t in hist:
            msgs.append({"role": role, "content": t})
        # --- the final user message: summary, memories, rooms detail, current text
        mem_text = ""
        for m in memories or []:
            line = clean_user_text(m, 200)
            if line and est_tokens(mem_text + line + "\n") <= b.memories:
                mem_text += line + "\n"
        mem_text = mem_text.rstrip("\n")
        cur = truncate_tokens(clean_user_text(user_text, 400), b.current)
        detail = rooms_detail(self.registry, cur, b.devices, self.aliases) if self.degraded else ""
        parts = []
        if summary:
            parts.append("Earlier: " + summary)
        if mem_text:
            parts.append("Known about the user:\n" + mem_text)
        if detail:
            parts.append(detail)
        parts.append("User: " + cur)
        msgs.append({"role": "user", "content": "\n".join(parts)})
        tr = truncate_tokens(tool_results, b.tool_results) if tool_results else ""
        for m in extra_tail or []:
            msgs.append(m)
        if tr:
            msgs.append({"role": "user", "content": "Tool results:\n" + tr})
        sizes = {"system": est_tokens(self.system_prompt), "tools": est_tokens(TOOLS_TEXT), "devices": self.devices_tokens,
                 "memories": est_tokens(mem_text), "recent": recent_tokens, "current": est_tokens(cur),
                 "tool_results": est_tokens(tr), "say": est_tokens(self.say_text)}
        p = Prompt(msgs, sizes, self.prefix_hash, b, self.degraded)
        p.check()
        return p


# --------------------------------------------------------------------------------- tool results shown to the model
def render_result(tool: str, args: dict, result: dict, reg: DeviceRegistry, aliases: AliasMap) -> str:
    """One compact line per result, rebuilt from validated fields only. Device-supplied free text never passes.
    Endpoints are named by alias (S8i), matching the compact wire format the model reads them back as."""
    def nm(ep: object) -> str:
        d = reg.devices.get(ep) if isinstance(ep, str) else None
        return f"{aliases.alias(ep)}({d.name})" if d else "unknown"

    def val(v: object) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v if isinstance(v, str) and ENUM_VAL_RE.match(v) else "?"   # enum values are [a-z0-9_]{1,24}

    if result.get("ok") is False and "error" in result and tool not in ("run_scene",):
        return f"{tool}: error {clean_label(result.get('error'), 32)}"
    if tool == "set_state":
        v = "verified" if result.get("verified") else "not verified"
        return f"set_state {nm(result.get('endpoint_id'))} {clean_label(result.get('capability'), 24)}={val(result.get('value'))}: {clean_label(result.get('status'), 12)}, {v}"
    if tool == "get_state":
        out = []
        for s in (result.get("states") or [])[:20]:
            if not isinstance(s, dict):
                continue
            vals = " ".join(f"{clean_label(k, 24)}={val(v)}" for k, v in sorted((s.get("values") or {}).items())[:8])
            out.append(f"{nm(s.get('endpoint_id'))} {'online' if s.get('online') else 'OFFLINE'} {vals}")
        return "get_state: " + "; ".join(out)
    if tool == "read_sensor":
        stale = " (stale)" if result.get("stale") else ""
        return f"read_sensor {nm(result.get('endpoint_id'))} {clean_label(result.get('capability'), 24)}={val(result.get('value'))}{clean_label(result.get('unit'), 8)}{stale}"
    if tool == "list_devices":
        return "list_devices: " + "; ".join(device_line(d, aliases.alias(d.endpoint_id)) for d in list(reg.devices.values())[:20])
    if tool == "run_scene":
        if result.get("status") == "needs_confirmation":
            return "run_scene: needs confirmation, nothing executed"
        if result.get("error"):
            return f"run_scene: error {clean_label(result.get('error'), 32)}"
        res = [f"{nm(r.get('endpoint_id'))} {clean_label(r.get('capability'), 24)} {clean_label(r.get('status'), 24)}"
               for r in (result.get("results") or [])[:20] if isinstance(r, dict)]
        return "run_scene: " + "; ".join(res)
    return f"{tool}: done"
