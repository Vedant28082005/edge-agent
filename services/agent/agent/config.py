"""Configuration: defaults, overridden by a JSON file (--config). Secrets are only ever referenced by file path."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class AgentConfig:
    host: str = "127.0.0.1"                   # internal service: loopback only (ARCHITECTURE section 2)
    port: int = 8200
    allow_non_loopback: bool = False          # container deploys only: bind 0.0.0.0 inside a private (unpublished) network
    hub_url: str = "http://127.0.0.1:8100"
    hub_secret_file: str = "/etc/edge-agent/secrets/agent.secret"   # mode 600, owned by the agent user; never env/argv
    llm_url: str = "http://127.0.0.1:8080"
    llm_model: str = "local"
    llm_max_tokens: int = 256
    llm_timeout_s: float = 600.0
    constrained: bool = True                  # runtime switch: constrained decoding on/off (S10 ablation)
    db_path: str = "agent.sqlite"
    api_verifier_file: str = "/etc/edge-agent/secrets/verifiers/agent-api.sha256"   # SHA-256 of the gateway secret
    max_steps: int = 4                        # LLM rounds per request
    max_calls: int = 8                        # tool calls per request
    deadline_s: float = 180.0                 # wall clock per request, confirmation waits excluded
    max_retries: int = 1                      # idempotent set_state retries (ack_timeout / hub unreachable)
    confirmation_ttl_s: float = 60.0
    settle_wait_s: float = 150.0              # how long a stream stays open after `final` for settle events
    max_text_chars: int = 4000
    max_queued_turns: int = 2
    scene_names: list[str] = field(default_factory=list)   # no tool lists scenes; optional admin-provided names
    include_state_events: bool = False


def load_config(path: str | None) -> AgentConfig:
    cfg = AgentConfig()
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in fields(AgentConfig)}
        for k, v in data.items():
            if k not in known:
                raise ValueError(f"unknown config key: {k}")
            setattr(cfg, k, v)
    if cfg.host not in ("127.0.0.1", "::1", "localhost") and not cfg.allow_non_loopback:
        raise ValueError("the agent API is internal: host must be a loopback address "
                         "(allow_non_loopback only for a container on a private network that does not publish the port)")
    return cfg
