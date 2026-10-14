"""Internal event bus. Every input is an Event: user message, hub events, perception events, node health.
v1 reacts to user messages (chat) and keeps the registry/settle subscribers; more subscribers can be added later
(`bus.subscribe`). Handlers run as their own tasks so a slow one (a whole LLM turn) never blocks dispatch."""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("agent.bus")

# Event types (the namespace is open; these are the ones v1 publishes).
USER_MESSAGE = "user_message"
HUB_REGISTRY_CHANGED = "hub.registry_changed"
HUB_NODE_STATUS = "hub.node_status"
HUB_STATE = "hub.state"
HUB_SETTLED = "hub.action_settled"
HUB_SETTLE_TIMEOUT = "hub.action_settle_timeout"
PERCEPTION = "perception"          # reserved: published by S8 when yolo/vision events arrive
NODE_HEALTH = "node_health"        # reserved: services/{name}/health


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "internal"
    ts: float = field(default_factory=time.time)
    user_id: str | None = None


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, str, Handler]] = []     # (pattern, name, handler)
        self._tasks: set[asyncio.Task] = set()
        self.dispatched = 0

    def subscribe(self, pattern: str, handler: Handler, name: str | None = None) -> None:
        """`pattern` is an fnmatch pattern on the event type: 'user_message', 'hub.*', '*'."""
        self._subs.append((pattern, name or getattr(handler, "__qualname__", "handler"), handler))

    def publish(self, event: Event) -> int:
        """Schedule every matching handler as a task; returns how many matched. Never raises into the publisher."""
        n = 0
        for pattern, name, handler in list(self._subs):
            if fnmatch.fnmatchcase(event.type, pattern):
                n += 1
                t = asyncio.get_running_loop().create_task(self._run(name, handler, event))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        self.dispatched += 1
        return n

    async def _run(self, name: str, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handler %s failed for event %s", name, event.type)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)


class RulesEngine:
    """Stub rules engine (hook, D13). Subscribes to the bus; the `rules` table is empty in v1, so it never acts.
    A rule is {id, name, event_type, when (optional equality checks on event.data), actions:[{tool, args}]}; its
    actions go through the same executor with trigger kind 'rule', which means `risk: confirm` endpoints only ever
    produce a notification (policy.needs_confirmation), never a prompt or an execution."""

    def __init__(self, store: Any, run_action: Callable[[dict, str, str, str], Awaitable[Any]] | None = None) -> None:
        self.store = store
        self.run_action = run_action
        self.matched = 0

    def attach(self, bus: EventBus) -> None:
        bus.subscribe("*", self.on_event, "rules_engine")

    async def on_event(self, event: Event) -> None:
        if event.type == USER_MESSAGE:
            return                                  # chat is not a trigger
        for rule in self.store.enabled_rules():
            if rule["event_type"] != event.type:
                continue
            when = rule.get("when") or {}
            if any(event.data.get(k) != v for k, v in when.items()):
                continue
            self.matched += 1
            if self.run_action is None:
                continue
            for action in rule.get("actions", []):
                await self.run_action(action, f"rule:{rule['id']}", f"rule '{rule.get('name', rule['id'])}' on {event.type}",
                                      str(rule.get("name", rule["id"])))
