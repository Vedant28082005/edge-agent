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
from .evring import EventRing, RingNotifier
from .notify import Notifier
from .planner import Planner
from .policy import Trigger
from .prompt import PromptBuilder
from .reqctx import Budget, ReqCtx
from .sched.config import SchedConfig
from .sched.grammar import make_backend
from .sched.scheduler import Scheduler
from .util import action_hash
from .settle import SettleTable
from .store import Store
from .util import read_secret_file

log = logging.getLogger("agent")


class Agent:
    def __init__(self, cfg: AgentConfig, llm: Llm, hub: McpClient | None = None, stream: HubEventStream | None = None,
                 store: Store | None = None, notifier: Notifier | None = None) -> None:
        self.cfg = cfg
        self.metrics = Metrics()
        self.ring = EventRing()
        self.notifier = notifier or RingNotifier(self.ring)
        self.store = store or Store(cfg.db_path)
        self._bg: set[asyncio.Task] = set()
        self.mock = False
        self.registry = DeviceRegistry()
        self.bus = EventBus()
        self.llm = llm
        self.sched_cfg = SchedConfig.from_dict(cfg.sched)
        self.sched_cfg.constrained_decoding = self.sched_cfg.constrained_decoding and cfg.constrained
        self.llm.constrained = self.sched_cfg.constrained_decoding
        self.llm.constraint = make_backend(self.sched_cfg.constraint_backend)
        self.llm.prefix_cache, self.llm.id_slot = self.sched_cfg.prefix_cache, self.sched_cfg.id_slot
        self.hub = hub
        self.builder = PromptBuilder(self.registry)
        self.loader = RegistryLoader(hub, self.registry, self._on_registry_changed) if hub else None
        self.confirmations = ConfirmationManager(cfg.confirmation_ttl_s)
        self.settles = SettleTable(self.notifier, cfg.settle_wait_s)
        self.executor = Executor(hub, self.registry, self.store, self.confirmations, self.settles, self.notifier,
                                 self.metrics, tuple(cfg.scene_names)) if hub else None
        self.planner = Planner(llm, self.plan_schema, self.metrics)
        self.sched = Scheduler(self.sched_cfg, self.registry, self.executor, self.store, self.bus,
                               lambda: tuple(cfg.scene_names))
        self.chat = ChatService(self.registry, self.builder, self.planner, self.executor, self.store, self.metrics,
                                cfg.settle_wait_s, cfg.max_queued_turns, self.sched) if self.executor else None
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
        """Constrained-decoding schema (+ GBNF), rebuilt only when the registry's decoding-relevant content changed."""
        return self.sched.schema()

    def set_flags(self, **flags: bool) -> None:
        """Runtime ablation switches (S10): fast_path, router, constrained_decoding, prefix_cache."""
        if "constrained" in flags:                       # v1 name of constrained_decoding
            flags["constrained_decoding"] = flags.pop("constrained")
        self.sched.set_flags(**flags)
        self.llm.constrained = self.sched_cfg.constrained_decoding
        self.llm.prefix_cache = self.sched_cfg.prefix_cache

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
    def new_request(self, user_id: str, session_id: str, text: str, role: str = "member", images: int = 0) -> ReqCtx:
        """`role` comes only from the authenticated gateway call (app.py); it is never derived from the user id."""
        c = self.cfg
        trig = Trigger("user", user_id=user_id, guest=(role == "guest"))
        ctx = ReqCtx(user_id, session_id, text, trig, Budget(c.max_steps, c.max_calls, c.deadline_s, c.max_retries),
                     role=role, images=images, sink=self._to_ring)
        self.bus.publish(Event(USER_MESSAGE, {"ctx": ctx}, source="chat", user_id=user_id))
        return ctx

    def _to_ring(self, name: str, data: dict, user_id: str | None) -> None:
        self.ring.publish(name, data, user_id)

    # ------------------------------------------------------------------------------------------ confirmation hand-off
    async def handle_decision(self, request_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/confirmations/{request_id} (body already schema-validated by app.py). The token is used for one call
        and dropped; it is never logged, stored or put in an event. Returns (http status, json body)."""
        user_id, decision, role = body["user_id"], body["decision"], body["role"]
        action, claimed = body["action"], body["action_hash"]
        token = body.get("confirmation_token")
        cm = self.confirmations
        if role == "guest":
            return 400, {"error": "bad_request", "message": "guests have no confirmations"}
        try:
            computed = action_hash(action)
        except (TypeError, ValueError):
            return 400, {"error": "bad_request", "message": "action cannot be canonicalised"}
        if cm.owner_of(request_id) not in (None, user_id):
            return 404, {"error": "unknown_request"}                 # another user's request: same answer as unknown
        if decision == "approved":
            if computed != claimed:
                log.warning("confirmation %s refused: action_hash mismatch", request_id)
                return 409, {"error": "action_hash_mismatch", "message": "nothing executed"}
            pend = cm.pending_hash(request_id, user_id)
            if cm.knows(request_id, user_id):
                if pend is not None and pend != claimed:
                    return 409, {"error": "action_hash_mismatch", "message": "does not match the pending request"}
                cm.resolve(request_id, user_id, True, token)
                return 202, {"accepted": True}
            if cm.is_handled(request_id):
                return 202, {"accepted": True, "note": "already handled"}
            # unknown here: a Retry request only the gateway created. The body carries the full action; run exactly that.
            self._spawn(self._run_standalone(user_id, request_id, action, token))
            return 202, {"accepted": True}
        # declined / expired: no token; clear pending state
        if cm.knows(request_id, user_id):
            cm.resolve(request_id, user_id, False) if decision == "declined" else cm.expire(request_id, user_id)
            return 202, {"accepted": True}
        if cm.is_handled(request_id):
            return 202, {"accepted": True, "note": "already handled"}
        return 404, {"error": "unknown_request"}

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _run_standalone(self, user_id: str, request_id: str, action: dict, token: str) -> None:
        c = self.cfg
        ctx = ReqCtx(user_id, "confirmation", "confirmed action", Trigger("user", user_id=user_id),
                     Budget(1, 1, 60.0, c.max_retries), role="member", sink=self._to_ring)
        ctx.reason = f"user confirmed request {request_id}"
        ctx.detached = True                                         # no chat stream: late events go to the ring
        try:
            out = await self.executor.run_approved(ctx, action, token)
            token = None
            from .chat import compose_final
            text = compose_final("Done.", [out], self.registry)
            await self.notifier.notify(user_id, "action_result", text)
        except Exception:
            log.exception("approved action failed")

    # ------------------------------------------------------------------------------------------ health
    async def health(self) -> dict:
        llm = "mock" if getattr(self.llm, "mock", False) else ("up" if await self.llm.health() else "down")
        if self.mock:
            hub = "mock"
        else:
            try:
                await self.hub.list_tools()
                hub = "up"
            except Exception:
                hub = "down"
        return {"ok": llm in ("up", "mock") and hub in ("up", "mock"), "llm": llm, "hub": hub}

    async def _run_rule_action(self, action: dict, trigger_ref: str, reason: str, name: str = ""):
        """Rules engine hook: a rule's action runs through the same executor, with a non-user trigger."""
        c = self.cfg
        ctx = ReqCtx("rule", "rule", reason, Trigger("rule", ref=trigger_ref),
                     Budget(1, 1, 30.0, c.max_retries), sink=self._to_ring)
        ctx.reason, ctx.label = reason[:200], name
        out = await self.executor.run_call(ctx, action.get("tool"), action.get("args"))
        ctx.close()
        return out

    def stats(self) -> dict:
        return {"constrained": self.llm.constrained, "sched": self.sched.stats(), "prefix_hash": self.builder.prefix_hash,
                "prefix_version": self.builder.prefix_version, "devices": len(self.registry.devices),
                "registry_version": self.registry.version, "metrics": self.metrics.as_dict(),
                "pending_settles": len(self.settles),
                "event_stream": {"connected": bool(self.stream and self.stream.connected),
                                 "last_id": self.stream.last_id if self.stream else None,
                                 "reconnects": self.stream.reconnects if self.stream else 0}}
