"""Node configuration (non-secret JSON, --config). Sources are configured here and nowhere else: the API can only
read the list and run a detection (no admin operations, no URL or path ever comes from a caller)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

NODE_RE = re.compile(r"^[a-z0-9-]{1,32}$")
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,48}$")
SOURCE_TYPES = ("usb", "csi", "rtsp", "http_snapshot", "folder_replay")
BACKENDS = ("mock", "onnx", "trt")


@dataclass
class Config:
    node: str = "jetson-yolo"
    host: str = "127.0.0.1"              # bind the node's Tailscale address; 0.0.0.0 only with allow_wildcard_bind
    port: int = 8400
    allow_wildcard_bind: bool = False    # container only: compose publishes the port on the tailnet address
    verifier_file: str = "/etc/edge-agent/secrets/verifiers/agent.sha256"
    backend: str = "onnx"
    model_name: str = "yolov8n"
    model_path: str = "/var/lib/edge-agent/yolo-node/models/yolov8n.onnx"
    names_file: Optional[str] = None     # one class name per line; default: COCO-80
    input_size: int = 640
    threads: int = 2
    labels_allow: Optional[List[str]] = None   # optional extra allowlist on top of the label regex
    trt: Dict[str, Any] = field(default_factory=lambda: {
        "engine_path": "/var/lib/edge-agent/yolo-node/models/yolov8n-fp16.engine",
        "worker_python": "/usr/bin/python3",          # JetPack 4.6: system Python 3.6 owns the tensorrt bindings
        "worker_script": "",                           # default: the shipped backends/trt_worker.py
        "start_timeout_s": 180,
        "io_timeout_s": 30,
    })
    sources: List[Dict[str, Any]] = field(default_factory=list)


def load_config(path: Optional[str]) -> Config:
    raw: Dict[str, Any] = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise SystemExit("config must be a JSON object")
    cfg = Config()
    for k, v in raw.items():
        if not hasattr(cfg, k):
            raise SystemExit("unknown config key %r" % k)
        if k == "trt":
            cfg.trt.update(v)
        else:
            setattr(cfg, k, v)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if not NODE_RE.match(cfg.node):
        raise SystemExit("config node must match [a-z0-9-]{1,32}")
    if cfg.backend not in BACKENDS:
        raise SystemExit("config backend must be one of %s" % (BACKENDS,))
    if not MODEL_RE.match(cfg.model_name):
        raise SystemExit("config model_name must match [A-Za-z0-9._-]{1,48}")
    if cfg.host in ("0.0.0.0", "::", "") and not cfg.allow_wildcard_bind:
        raise SystemExit("refusing to bind a wildcard address: set host to the Tailscale address "
                         "(allow_wildcard_bind only inside the container, where compose publishes on the tailnet IP)")
    if not isinstance(cfg.port, int) or not (1 <= cfg.port <= 65535):
        raise SystemExit("config port invalid")
    if len(cfg.sources) > 8:
        raise SystemExit("at most 8 sources")
    seen = set()
    for s in cfg.sources:
        sid, typ = s.get("source_id"), s.get("type")
        if not isinstance(sid, str) or not SOURCE_ID_RE.match(sid) or sid in seen:
            raise SystemExit("source_id invalid or duplicate")
        if typ not in SOURCE_TYPES:
            raise SystemExit("source type must be one of %s" % (SOURCE_TYPES,))
        seen.add(sid)
