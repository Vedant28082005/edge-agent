"""Planner: one LLM round -> a Plan (a list of proposed tool calls + a one-sentence `say`). It never executes anything;
the executor validates and runs the calls. The reply is parsed leniently (unconstrained mode may wrap or truncate JSON)
but only `{"tool": str, "args": dict}` items survive, at most MAX_CALLS."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .executor import Metrics
from .llm import Llm, LlmResult
from .schema import MAX_CALLS, SAY_MAX
from .scope import UNRESTRICTED, Scope
from .util import clean_user_text

log = logging.getLogger("agent.planner")


@dataclass
class Plan:
    calls: list[dict]
    say: str
    parsed_ok: bool
    timings: dict = field(default_factory=dict)

    def as_assistant_json(self) -> str:
        return json.dumps({"calls": self.calls, "say": self.say}, separators=(",", ":"), ensure_ascii=False)


def _first_object(text: str) -> object | None:
    start = text.find("{")
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_plan(text: str) -> Plan:
    obj: object | None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        obj = _first_object(text or "")
    if not isinstance(obj, dict):
        return Plan([], clean_user_text(text or "", SAY_MAX), False)
    calls_raw = obj.get("calls") if isinstance(obj.get("calls"), list) else []
    calls = [{"tool": c.get("tool"), "args": c.get("args")} for c in calls_raw[:MAX_CALLS]
             if isinstance(c, dict) and isinstance(c.get("tool"), str)]
    say = obj.get("say") if isinstance(obj.get("say"), str) else ""
    return Plan(calls, clean_user_text(say, SAY_MAX), True)


class Planner:
    def __init__(self, llm: Llm, schema_provider, metrics: Metrics) -> None:
        self.llm, self._schema, self.metrics = llm, schema_provider, metrics

    async def plan(self, messages: list[dict], scope: Scope = UNRESTRICTED) -> Plan:
        schema = self._schema(scope) if self.llm.constrained else None
        res: LlmResult = await self.llm.complete(messages, schema)
        plan = parse_plan(res.content)
        plan.timings = res.timings
        self.metrics.plans += 1
        if not plan.parsed_ok:
            self.metrics.plan_parse_failures += 1
            log.warning("planner reply was not a JSON plan")
        return plan
