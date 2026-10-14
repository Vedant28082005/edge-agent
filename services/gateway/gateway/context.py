"""Wiring: everything the two apps share (one DB, one confirmation service, one broadcaster)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .auth import AuthService, LoginLimiter
from .confirmations import Confirmations
from .config import Config
from .db import DB
from .events import Broadcaster, run_agent_subscriber, run_hub_subscriber
from .limits import RateLimiter, WsTickets
from .media import MediaStore
from .profiles import WifiProfiles
from .security import SECRETS, Jwt, install_log_redaction, load_or_create_key
from .upstream import AgentClient, HubClient, JsonClient, read_secret_file

log = logging.getLogger("gateway")


@dataclass
class Ctx:
    cfg: Config
    db: DB
    auth: AuthService
    hub: HubClient
    agent: AgentClient
    vision: JsonClient
    yolo: JsonClient
    media: MediaStore
    bc: Broadcaster
    confirm: Confirmations
    profiles: WifiProfiles
    limiter: RateLimiter
    tickets: WsTickets
    clock: object = time.time
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list = field(default_factory=list)
    agent_push: bool = True                       # subscribe to the agent GET /v1/events (agent-internal.yaml)
    autostart: bool = False                       # start background subscribers from the app lifespan

    def start(self, retry_s: float = 1.0) -> None:
        self.stop = asyncio.Event()
        self.tasks = [asyncio.create_task(run_hub_subscriber(self.hub, self.bc, self.stop, retry_s, self.confirm.assigned))]
        if self.agent_push:
            self.tasks.append(asyncio.create_task(run_agent_subscriber(self.agent, self.bc, self.confirm, self.stop, retry_s)))
        self.tasks.append(asyncio.create_task(self._sweeper(self.stop)))

    async def _sweeper(self, stop: asyncio.Event) -> None:
        """Expired confirmation requests: tell the agent and the user (agent-events.md, 'declined and expired are delivered too')."""
        while not stop.is_set():
            try:
                await self.confirm.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("confirmation sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), 2.0)
            except asyncio.TimeoutError:
                pass

    async def shutdown(self) -> None:
        self.stop.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        for c in (self.hub, self.agent, self.vision, self.yolo):
            await c.aclose()


def build_ctx(cfg: Config, *, transports: dict | None = None, hub_secret: str | None = None, agent_secret: str | None = None, clock=time.time,
              db_path: str | None = None, agent_push: bool = True) -> Ctx:
    """`transports` (tests, --mock): per-upstream httpx transports, so nothing touches the network."""
    tr = transports or {}
    cfg.data.mkdir(parents=True, exist_ok=True)
    db = DB(db_path or str(cfg.data / "gateway.db"), clock)
    jwt_ = Jwt(load_or_create_key(cfg.data / "jwt.key"), cfg.access_ttl_s, clock)
    limiter = LoginLimiter(cfg.login_max_failures, cfg.login_max_failures_ip, cfg.login_window_s, clock)
    auth = AuthService(db, jwt_, cfg.refresh_ttl_s, limiter)
    agent_secret = read_secret_file(cfg.agent_secret_file, "gateway->agent") if agent_secret is None else agent_secret
    if agent_secret:
        SECRETS.add(agent_secret)
    if hub_secret is not None:
        SECRETS.add(hub_secret)
    secret = hub_secret if hub_secret is not None else read_secret_file(cfg.hub_secret_file, "gateway->hub")
    hub = HubClient(cfg.hub.url, secret, cfg.hub.timeout_s, tr.get("hub"))
    agent = AgentClient(cfg.agent.url, agent_secret, cfg.agent.timeout_s, tr.get("agent"))
    vision = JsonClient("vision-node", cfg.vision.url, cfg.vision.timeout_s, None, tr.get("vision"))
    yolo = JsonClient("yolo-node", cfg.yolo.url, cfg.yolo.timeout_s, None, tr.get("yolo"))
    bc = Broadcaster(cfg.ws_queue)
    media = MediaStore(cfg.media_ttl_s, cfg.media_max_total_bytes, cfg.media_max_per_user, clock)
    confirm = Confirmations(db, hub, agent, bc, auth, cfg.confirm_ttl_s)
    profiles = WifiProfiles(db, load_or_create_key(cfg.data / "wifi.key", fernet=True))
    install_log_redaction()
    limiter_ = RateLimiter({k: tuple(v) for k, v in cfg.rate_limits.items()}, clock)
    tickets = WsTickets(cfg.ticket_ttl_s, clock)
    return Ctx(cfg, db, auth, hub, agent, vision, yolo, media, bc, confirm, profiles, limiter_, tickets, clock, agent_push=agent_push)
