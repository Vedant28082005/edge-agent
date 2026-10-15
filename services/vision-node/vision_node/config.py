"""Non-secret JSON configuration (deploy/config/vision-node.json). Secrets and verifiers are files, never config values."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path

_NODE = re.compile(r"[a-z0-9-]{1,32}")
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,48}")


@dataclass
class Config:
    node: str = "pi-vision"
    host: str = "127.0.0.1"                 # production: the node's Tailscale address (never 0.0.0.0)
    port: int = 8300
    verifier_file: str = "/etc/edge-agent/secrets/verifiers/agent.sha256"
    contracts_dir: str | None = None
    llama_url: str = "http://127.0.0.1:8081"
    llama_model_name: str = "qwen3-vl-2b-q4"
    whisper_url: str = "http://127.0.0.1:8082"
    whisper_model_name: str = "whisper-base.en"
    yolo_model_path: str = "/var/lib/edge-agent/models/yolov8n.onnx"
    yolo_model_name: str = "yolov8n"
    ffmpeg: str = "ffmpeg"
    min_mem_mb: int = 400
    max_waiting: int = 4
    node_cap_ms: int = 25000
    cache_ttl_s: float = 30.0

    def validate(self, mock: bool) -> "Config":
        if not _NODE.fullmatch(self.node):
            raise SystemExit("vision-node: bad node name")
        for m in (self.llama_model_name, self.whisper_model_name, self.yolo_model_name):
            if not _MODEL.fullmatch(m):
                raise SystemExit("vision-node: bad model name")
        if not mock and self.host in ("", "0.0.0.0", "::", "[::]"):
            raise SystemExit("vision-node: refusing to bind a wildcard address; use the node's Tailscale address")
        if not 1 <= self.max_waiting <= 4 or not 1000 <= self.node_cap_ms <= 25000:
            raise SystemExit("vision-node: max_waiting must be 1..4 and node_cap_ms 1000..25000")
        return self


def load_config(path: str | None) -> Config:
    if not path:
        return Config()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {f.name for f in fields(Config)}
    flat: dict = {}
    for k, v in raw.items():
        if isinstance(v, dict) and k in ("http", "auth", "llama", "whisper", "yolo", "limits"):
            for kk, vv in v.items():
                flat[{"http": "", "auth": "", "llama": "llama_", "whisper": "whisper_", "yolo": "yolo_", "limits": ""}[k] + kk] = vv
        else:
            flat[k] = v
    unknown = set(flat) - names
    if unknown:
        raise SystemExit(f"vision-node: unknown config keys: {sorted(unknown)}")
    return Config(**flat)
