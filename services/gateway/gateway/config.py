"""Configuration. Secrets are referenced by file path only, never stored in the config, env or argv."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class Listener:
    host: str = "127.0.0.1"
    port: int = 8000
    uds: str | None = None          # internal listener only: preferred unix socket path (Linux)


@dataclass
class Upstream:
    url: str = ""
    timeout_s: float = 5.0


@dataclass
class Config:
    data_dir: str = "./data"                      # gateway.db, jwt.key, wifi.key (git-ignored)
    static_dir: str = ""                          # built web app; empty = bundled placeholder
    public: Listener = field(default_factory=lambda: Listener(port=8000))
    internal: Listener = field(default_factory=lambda: Listener(port=8001))
    hub: Upstream = field(default_factory=lambda: Upstream("http://127.0.0.1:8100"))
    agent: Upstream = field(default_factory=lambda: Upstream("http://127.0.0.1:8200", 30.0))
    vision: Upstream = field(default_factory=lambda: Upstream("http://pi-vision:8300", 30.0))
    yolo: Upstream = field(default_factory=lambda: Upstream("http://jetson:8400", 3.0))
    hub_secret_file: str = "/etc/edge-agent/secrets/gateway.secret"
    agent_secret_file: str = "/etc/edge-agent/secrets/agent-api.secret"     # gateway -> agent (agent-internal.yaml)
    verifier_dir: str = "/etc/edge-agent/secrets/verifiers"   # <service>.sha256 (device-hub.sha256)
    access_ttl_s: int = 900
    refresh_ttl_s: int = 14 * 86400
    confirm_ttl_s: int = 60                       # default TTL, server-configurable up to 300
    login_max_failures: int = 5                   # per username+ip per window
    login_max_failures_ip: int = 20               # per ip per window
    login_window_s: int = 900
    max_image_bytes: int = 5 * 1024 * 1024
    max_audio_bytes: int = 10 * 1024 * 1024
    media_ttl_s: int = 120
    media_max_total_bytes: int = 32 * 1024 * 1024
    media_max_per_user: int = 8
    ws_queue: int = 256
    ticket_ttl_s: int = 30
    ws_auth_timeout_s: float = 5.0                # first-message auth deadline
    # per-user request limits {name: [max requests, window seconds]}; exceeding -> 429 + Retry-After
    rate_limits: dict = field(default_factory=lambda: {"chat": [30, 60], "voice": [20, 60], "confirm": [30, 60],
                                                       "ticket": [10, 60], "ws": [20, 60]})
    mock: bool = False

    def __post_init__(self) -> None:
        self.confirm_ttl_s = max(1, min(int(self.confirm_ttl_s), 300))

    @property
    def data(self) -> Path:
        return Path(self.data_dir)


def _merge(obj, data: dict):
    unknown = set(data) - {f.name for f in fields(obj)}
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for f in fields(obj):
        if f.name not in data:
            continue
        cur, val = getattr(obj, f.name), data[f.name]
        if is_dataclass(cur) and isinstance(val, dict):
            _merge(cur, val)
        else:
            setattr(obj, f.name, val)
    return obj


def load_config(path: str | None) -> Config:
    cfg = Config()
    if path:
        _merge(cfg, json.loads(Path(path).read_text(encoding="utf-8")))
    cfg.__post_init__()
    return cfg
