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
from .media import MediaStore
from .profiles import WifiProfiles
from .security import Jwt, install_log_redaction, load_or_create_key
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
    clock: object = time.time
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list = field(default_factory=list)
    agent_push: bool = False

    def start(self, retry_s: float = 1.0) -> None:
        self.stop = asyncio.Event()
        self.tasks = [asyncio.create_task(run_hub_subscriber(self.hub, self.bc, self.stop, retry_s))]
        if self.agent_push:
            self.tasks.append(asyncio.create_task(run_agent_subscriber(self.agent, self.bc, self.stop)))

    async def shutdown(self) -> None:
        self.stop.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        for c in (self.hub, self.agent, self.vision, self.yolo):
            await c.aclose()


def build_ctx(cfg: Config, *, transports: dict | None = None, hub_secret: str | None = None, clock=time.time,
              db_path: str | None = None, agent_push: bool = False) -> Ctx:
    """`transports` (tests, --mock): per-upstream httpx transports, so nothing touches the network."""
    tr = transports or {}
    cfg.data.mkdir(parents=True, exist_ok=True)
    db = DB(db_path or str(cfg.data / "gateway.db"), clock)
    jwt_ = Jwt(load_or_create_key(cfg.data / "jwt.key"), cfg.access_ttl_s, clock)
    limiter = LoginLimiter(cfg.login_max_failures, cfg.login_max_failures_ip, cfg.login_window_s, clock)
    auth = AuthService(db, jwt_, cfg.refresh_ttl_s, limiter)
    if hub_secret is not None:
        from .security import SECRETS
        SECRETS.add(hub_secret)
    secret = hub_secret if hub_secret is not None else read_secret_file(cfg.hub_secret_file, "gateway->hub")
    hub = HubClient(cfg.hub.url, secret, cfg.hub.timeout_s, tr.get("hub"))
    agent = AgentClient(cfg.agent.url, cfg.agent.timeout_s, tr.get("agent"))
    vision = JsonClient("vision-node", cfg.vision.url, cfg.vision.timeout_s, None, tr.get("vision"))
    yolo = JsonClient("yolo-node", cfg.yolo.url, cfg.yolo.timeout_s, None, tr.get("yolo"))
    bc = Broadcaster(cfg.ws_queue)
    media = MediaStore(cfg.media_ttl_s, cfg.media_max_total_bytes, cfg.media_max_per_user, clock)
    confirm = Confirmations(db, hub, agent, bc, cfg.confirm_ttl_s)
    profiles = WifiProfiles(db, load_or_create_key(cfg.data / "wifi.key", fernet=True))
    install_log_redaction()
    return Ctx(cfg, db, auth, hub, agent, vision, yolo, media, bc, confirm, profiles, clock, agent_push=agent_push)
