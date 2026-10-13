"""Hub facade: wires registry, MQTT bridge, dynsec client, provisioning, executor, scenes and admin together."""
from __future__ import annotations

import logging
import threading

from .admin import AdminService
from .bridge import MqttBridge
from .config import HubConfig, read_secret_file
from .contracts import Contracts
from .db import Database
from .dynsec import DynsecClient
from .events import EventBus
from .executor import Executor
from .gateway_client import GatewayClient, NoGateway
from .provisioning import Provisioning
from .registry import Registry
from .scenes import SceneService
from .transport import PahoTransport, Transport
from .util import SECRETS, Clock

log = logging.getLogger("device_hub")


class Hub:
    def __init__(self, cfg: HubConfig, transport: Transport, gateway: GatewayClient | NoGateway,
                 clock: Clock | None = None, db: Database | None = None) -> None:
        self.cfg = cfg
        self.clock = clock or Clock()
        self.contracts = Contracts(cfg.contracts_dir or None)
        self.db = db or Database(cfg.db_path)
        self.bus = EventBus()
        self.registry = Registry(self.db, self.contracts, self.bus, self.clock)
        self.transport = transport
        self.registry.link_up = lambda: bool(transport.connected)
        self.gateway = gateway
        self.dynsec = DynsecClient(transport, cfg.dynsec_timeout_s)
        self.provisioning = Provisioning(self.registry, self.dynsec, cfg, self.clock)
        self.bridge = MqttBridge(transport, self.registry, self.contracts, self.bus,
                                 on_announce_accepted=self.provisioning.on_announce)
        self.executor = Executor(self.registry, self.bridge, gateway, cfg, self.clock, self.bus)
        self.scenes = SceneService(self.registry, self.executor, self.bus, self.clock)
        self.admin = AdminService(self.registry, self.bridge, self.dynsec, self.provisioning, self.executor, self.scenes)
        self._stop = threading.Event()
        self._maint: threading.Thread | None = None

    @classmethod
    def from_config(cls, cfg: HubConfig) -> "Hub":
        m = cfg.mqtt
        password = read_secret_file(m.password_file) if m.password_file else ""
        SECRETS.add(password)
        transport = PahoTransport(m.host, m.port, m.username, password, m.client_id, ca_file=m.ca_file or None,
                                  tls=m.tls, keepalive=m.keepalive)
        try:
            gw: GatewayClient | NoGateway = GatewayClient.from_secret_file(cfg.gateway.url, cfg.gateway.secret_file,
                                                                          cfg.gateway.timeout_s)
        except OSError:
            log.warning("gateway secret file not readable; risk:confirm actions will stay blocked")
            gw = NoGateway()
        return cls(cfg, transport, gw)

    def start(self) -> None:
        self.transport.start()
        self._maint = threading.Thread(target=self._maintenance, name="maintenance", daemon=True)
        self._maint.start()

    def _maintenance(self) -> None:
        while not self._stop.wait(self.cfg.maintenance_interval_s):
            self.run_maintenance()

    def run_maintenance(self) -> None:
        """Provisioning TTL cleanup and retry of pending revocations. Safe to call from tests."""
        try:
            self.provisioning.cleanup_expired()
            self.admin.retry_pending_revocations()
        except Exception:  # noqa: BLE001
            log.exception("maintenance failed")

    def stop(self) -> None:
        self._stop.set()
        self.scenes.close()
        try:
            self.transport.stop()
        finally:
            self.gateway.close()
            self.db.close()
