"""Configuration. A JSON file (--config) overrides defaults; secrets are only ever referenced by file path."""
from __future__ import annotations

import json
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 8883
    ca_file: str = ""                 # CA certificate (public); required unless mock
    username: str = "hub-admin"
    password_file: str = ""           # file holding the hub's MQTT password (never env/argv)
    client_id: str = "device-hub"
    keepalive: int = 30
    tls: bool = True


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8100


@dataclass
class GatewayConfig:
    # http://127.0.0.1:8001 or unix:///run/edge-agent/gateway-internal.sock
    url: str = "http://127.0.0.1:8001"
    secret_file: str = "/etc/edge-agent/secrets/device-hub.secret"
    timeout_s: float = 2.0


@dataclass
class ProvisionConfig:
    broker_host: str = ""             # what the ESP32 connects to (LAN IP of pi-brain); required for sessions
    broker_port: int = 8883
    ntp_host: str = ""                # optional; defaults to broker_host
    ca_file: str = ""                 # CA put in the bundle; defaults to mqtt.ca_file
    ttl_s: int = 600


@dataclass
class AuthConfig:
    # Directory with <service>.sha256 verifier files (hex SHA-256 of the caller's secret). Empty = no caller auth
    # (loopback-only dev). Scopes: gateway = everything except /mcp; agent = /mcp and GET /events only.
    verifier_dir: str = ""


@dataclass
class HubConfig:
    db_path: str = "device-hub.sqlite"
    contracts_dir: str = ""
    ack_timeout_s: float = 3.0
    dynsec_timeout_s: float = 5.0
    stale_factor: float = 3.0
    maintenance_interval_s: float = 15.0
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    provision: ProvisionConfig = field(default_factory=ProvisionConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)


def _fill(cls: type, data: dict[str, Any]):
    kwargs = {}
    known = {f.name: f for f in fields(cls)}
    for k, v in data.items():
        if k not in known:
            raise ValueError(f"unknown config key: {k}")
        f = known[k]
        default = f.default_factory() if f.default_factory is not MISSING else None
        kwargs[k] = _fill(type(default), v) if is_dataclass(default) else v
    return cls(**kwargs)


def load_config(path: str | None) -> HubConfig:
    if not path:
        return HubConfig()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _fill(HubConfig, data)


def read_secret_file(path: str) -> str:
    """Read a secret from a file (strip trailing newline). Never logged by callers."""
    return Path(path).read_text(encoding="utf-8").strip()
