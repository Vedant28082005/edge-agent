"""Planner: one LLM round -> a Plan (a list of proposed tool calls). It never executes anything; the executor
validates and runs the calls. The agent composes all reply text itself from verified results (`chat.compose_final` /
`describe_outcomes` / `no_plan_text`) -- the model has no text channel at all (S8i).

Wire format (S8i, compact): the reply is a bare JSON array of up to `MAX_CALLS` tuples, `[code, ...]` (`schema.TOOL_CODES`),
naming endpoints by a short per-turn alias instead of a full `endpoint_id` (`aliases.AliasMap`). Parsing is still lenient
(unconstrained mode may wrap or truncate the JSON) but only well-shaped, known-code tuples survive decoding back to the
internal `{"tool": str, "args": dict}` call shape every other module already works with; the executor's own validation
is the real backstop against a bad alias or a malformed tuple reaching hardware."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .aliases import AliasMap
from . import homebench
from .executor import Metrics
from .llm import Llm, LlmResult
from .schema import MAX_CALLS
from .scope import UNRESTRICTED, Scope

log = logging.getLogger("agent.planner")

# (code, arity incl. the code itself, tool, arg names in tuple order after the code)
_SHAPES: dict[str, tuple[int, str, tuple[str, ...]]] = {
    "s": (4, "set_state", ("endpoint_id", "capability", "value")),
    "r": (3, "read_sensor", ("endpoint_id", "capability")),
    "g": (2, "get_state", ("endpoint_id",)),
    "gr": (2, "get_state", ("room",)),
    "l": (3, "list_devices", ("room", "type")),
    "n": (2, "run_scene", ("scene",)),
}
_ALIASED_ARG = {"endpoint_id"}       # which arg position(s) carry an alias rather than a literal value


def _first_array(text: str) -> object | None:
    start = text.find("[")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("[", start + 1)
    return None


def _decode(tup: object, aliases: AliasMap) -> dict | None:
    if not isinstance(tup, list) or not tup or not isinstance(tup[0], str):
        return None
    shape = _SHAPES.get(tup[0])
    if shape is None:
        return None
    arity, tool, arg_names = shape
    if len(tup) != arity:
        return None
    args: dict = {}
    for name, v in zip(arg_names, tup[1:]):
        if name in _ALIASED_ARG:
            if not isinstance(v, str):
                return None
            # An alias outside this scope's map is forwarded as a literal, unresolved endpoint_id rather than
            # dropped: constrained decoding can never produce one (the enum only names known aliases), and under
            # the unconstrained ablation the executor's own validation (unknown_endpoint / out_of_scope) is still
            # the backstop -- same guarantee as before aliasing, just one token shorter on the wire.
            v = aliases.to_ep.get(v, v)
        if v is not None:                 # list_devices' room/type positions: null means "omitted"
            args[name] = v
    return {"tool": tool, "args": args}


def encode_call(call: dict, aliases: AliasMap) -> list | None:
    """The inverse of `_decode`, used only to re-show the planner's own previous turn back to it (a retry's `extra_tail`),
    so that context also stays in the compact wire format it was decoded from."""
    tool, args = call.get("tool"), call.get("args")
    if not isinstance(args, dict):
        return None
    for code, (_, t, arg_names) in _SHAPES.items():
        if t != tool:
            continue
        if tool == "list_devices":
            if not set(args) <= set(arg_names):
                continue
            vals = [args.get(n) for n in arg_names]
        else:
            if set(args) != set(arg_names):
                continue
            vals = [aliases.to_alias.get(args[n], args[n]) if n in _ALIASED_ARG else args[n] for n in arg_names]
        return [code, *vals]
    return None


@dataclass
class Plan:
    calls: list[dict]
    parsed_ok: bool
    timings: dict = field(default_factory=dict)
    raw: str = ""                   # the model's unparsed reply (S8f: logged, truncated, in turn_traces)
    failures: list[str] = field(default_factory=list)   # homebench only: "could not do this part" notes

    def as_assistant_json(self, aliases: AliasMap, hb_table: "homebench.HomebenchTable | None" = None) -> str:
        """The compact wire re-rendering of this plan, used only to show the planner its own previous turn in a
        retry. homebench format re-renders in the model's own `room.device.method(...)` notation (never the
        json-format's aliased tuples) when `hb_table` is given."""
        if hb_table is not None:
            return homebench.render_calls(self.calls, hb_table)
        tuples = [t for t in (encode_call(c, aliases) for c in self.calls) if t is not None]
        return json.dumps(tuples, separators=(",", ":"), ensure_ascii=False)


def parse_plan(text: str, aliases: AliasMap) -> Plan:
    obj: object | None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        obj = _first_array(text or "")
    if not isinstance(obj, list):
        return Plan([], False)
    calls = [c for c in (_decode(t, aliases) for t in obj[:MAX_CALLS]) if c is not None]
    return Plan(calls, True)


def parse_homebench_plan(text: str, table: homebench.HomebenchTable) -> Plan:
    res = homebench.parse_reply(text, table)
    return Plan(res.calls[:MAX_CALLS], True, failures=res.failures)


class Planner:
    def __init__(self, llm: Llm, schema_provider, metrics: Metrics, format: str = "json") -> None:
        """`schema_provider(scope)` returns the json-format's schema+aliases wrapper when `format == "json"`, or a
        `homebench.HomebenchTable` when `format == "homebench"` -- same call site, different return type per format."""
        self.llm, self._schema, self.metrics, self.format = llm, schema_provider, metrics, format

    async def plan(self, messages: list[dict], scope: Scope = UNRESTRICTED) -> Plan:
        if self.format == "homebench":
            table: homebench.HomebenchTable = self._schema(scope)
            res: LlmResult = await self.llm.complete(messages, None, max_tokens=128, repeat_penalty=1.0)
            plan = parse_homebench_plan(res.content, table)
        else:
            sch = self._schema(scope)                  # also carries the alias map: needed to decode even when unconstrained
            res = await self.llm.complete(messages, sch if self.llm.constrained else None)
            plan = parse_plan(res.content, sch.aliases)
        plan.timings = res.timings
        plan.raw = res.content or ""
        self.metrics.plans += 1
        if not plan.parsed_ok:
            self.metrics.plan_parse_failures += 1
            log.warning("planner reply was not a JSON plan")
        return plan
