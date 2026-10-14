"""Wires the agent together: hub client + event stream, registry cache, prompt builder, schema cache, planner, executor,
chat service, event bus, stub rules engine, store."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .chat import ChatService
from .confirmations import ConfirmationManager
from .config import AgentConfig
from .devices import DeviceRegistry, RegistryLoader
from .events import (HUB_NODE_STATUS, HUB_REGISTRY_CHANGED, HUB_SETTLE_TIMEOUT, HUB_SETTLED, HUB_STATE, USER_MESSAGE,
                     Event, EventBus, RulesEngine)
from .executor import Executor, Metrics
from .hub_client import HubEventStream, McpClient
from .llm import Llm
from .notify import LogNotifier, Notifier
from .planner import Planner
from .policy import Trigger
from .prompt import PromptBuilder
from .reqctx import Budget, ReqCtx
from .schema import build_plan_schema
from .settle import SettleTable
from .store import Store
from .util import read_secret_file

log = logging.getLogger("agent")


class Agent:
    def __init__(self, cfg: AgentConfig, llm: Llm, hub: McpClient | None = None, stream: HubEventStream | None = None,
                 store: Store | None = None, notifier: Notifier | None = None) -> None:
        self.cfg = cfg
        self.metrics = Metrics()
        self.notifier = notifier or LogNotifier()
        self.store = store or Store(cfg.db_path, cfg.guest_prefix)
        self.registry = DeviceRegistry()
        self.bus = EventBus()
        self.llm = llm
        self.llm.constrained = cfg.constrained
        self.hub = hub
        self.builder = PromptBuilder(self.registry)
        self._schema_gen = -1
        self._schema: dict = {}
        self.loader = RegistryLoader(hub, self.registry, self._on_registry_changed) if hub else None
        self.confirmations = ConfirmationManager(cfg.confirmation_ttl_s)
        self.settles = SettleTable(self.notifier, cfg.settle_wait_s)
        self.executor = Executor(hub, self.registry, self.store, self.confirmations, self.settles, self.notifier,
                                 self.metrics, tuple(cfg.scene_names)) if hub else None
        self.planner = Planner(llm, self.plan_schema, self.metrics)
        self.chat = ChatService(self.registry, self.builder, self.planner, self.executor, self.store, self.metrics,
                                cfg.settle_wait_s, cfg.max_queued_turns) if self.executor else None
        self.rules = RulesEngine(self.store, self._run_rule_action)
        self.stream = stream
        self._wire_bus()

    # ------------------------------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, cfg: AgentConfig, llm: Llm, http: httpx.AsyncClient | None = None) -> "Agent":
        secret = read_secret_file(cfg.hub_secret_file)
        hub = McpClient(cfg.hub_url, secret, http)
        agent = cls(cfg, llm, hub)
        agent.stream = HubEventStream(cfg.hub_url, secret, agent._on_hub_event, agent._on_hello,
                                      include_state=cfg.include_state_events)
        return agent

    def _wire_bus(self) -> None:
        self.rules.attach(self.bus)
        if self.chat:
            self.bus.subscribe(USER_MESSAGE, self.chat.on_user_message, "chat")
        self.bus.subscribe(HUB_REGISTRY_CHANGED, self._h_registry, "registry")
        self.bus.subscribe(HUB_NODE_STATUS, self._h_node_status, "node_status")
        self.bus.subscribe(HUB_SETTLED, self._h_settle, "settle")
        self.bus.subscribe(HUB_SETTLE_TIMEOUT, self._h_settle, "settle")

    # ------------------------------------------------------------------------------------------ schema / prefix
    def plan_schema(self) -> dict:
        """Constrained-decoding schema, rebuilt from the live registry whenever the registry cache was reloaded."""
        if self._schema_gen != self.registry.generation:
            self._schema = build_plan_schema(self.registry, tuple(self.cfg.scene_names))
            self._schema_gen = self.registry.generation
        return self._schema

    def _on_registry_changed(self) -> None:
        self.builder.rebuild_prefix()
        self.plan_schema()

    # ------------------------------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.hub:
            await self.hub.verify_tools()
            await self.loader.reload()
        if self.stream:
            self.stream.start()

    async def stop(self) -> None:
        if self.stream:
            await self.stream.stop()
        await self.bus.close()
        if self.hub:
            await self.hub.aclose()

    # ------------------------------------------------------------------------------------------ hub events -> bus
    async def _on_hub_event(self, etype: str, seq: int, data: dict) -> None:
        self.bus.publish(Event("hub." + etype, data, source="device-hub"))

    async def _on_hello(self, version: int | None, resync: bool) -> None:
        if resync or (version is not None and version != self.registry.version):
            await self.loader.reload(version)

    async def _h_registry(self, ev: Event) -> None:
        v = ev.data.get("version")
        await self.loader.reload(v if isinstance(v, int) else None)

    async def _h_node_status(self, ev: Event) -> None:
        await self.loader.reload()          # list_devices carries no node id, so refresh `online` by reloading

    async def _h_settle(self, ev: Event) -> None:
        await self.settles.on_hub_event(ev.type.removeprefix("hub."), ev.data)

    # ------------------------------------------------------------------------------------------ chat entry
    def new_request(self, user_id: str, session_id: str, text: str) -> ReqCtx:
        c = self.cfg
        trig = Trigger("user", user_id=user_id, guest=self.store.is_guest(user_id))
        ctx = ReqCtx(user_id, session_id, text, trig, Budget(c.max_steps, c.max_calls, c.deadline_s, c.max_retries))
        self.bus.publish(Event(USER_MESSAGE, {"ctx": ctx}, source="chat", user_id=user_id))
        return ctx

    async def _run_rule_action(self, action: dict, trigger_ref: str, reason: str):
        """Rules engine hook: a rule's action runs through the same executor, with a non-user trigger."""
        c = self.cfg
        ctx = ReqCtx("rule", "rule", reason, Trigger("rule", ref=trigger_ref),
                     Budget(1, 1, 30.0, c.max_retries))
        ctx.reason = reason[:200]
        out = await self.executor.run_call(ctx, action.get("tool"), action.get("args"))
        ctx.close()
        return out

    def stats(self) -> dict:
        return {"constrained": self.llm.constrained, "prefix_hash": self.builder.prefix_hash,
                "prefix_version": self.builder.prefix_version, "devices": len(self.registry.devices),
                "registry_version": self.registry.version, "metrics": self.metrics.as_dict(),
                "pending_settles": len(self.settles),
                "event_stream": {"connected": bool(self.stream and self.stream.connected),
                                 "last_id": self.stream.last_id if self.stream else None,
                                 "reconnects": self.stream.reconnects if self.stream else 0}}
