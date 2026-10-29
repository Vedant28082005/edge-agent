"""python -m vision_hub [--port 8310] [--llama-url http://127.0.0.1:8080] [--frames SPEC] [--allowed-dir DIR]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from .app import Service, create_app
from .frames import make_source
from .llama import LlamaClient


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vision_hub")
    ap.add_argument("--port", type=int, default=8310)
    ap.add_argument("--llama-url", default="http://127.0.0.1:8080")
    ap.add_argument("--frames", default="", help="frame source, e.g. folder:/home/vlmpi/edge-vision/frames")
    ap.add_argument("--allowed-dir", default="", help="directory callers may name by relative path")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds per VLM call")
    a = ap.parse_args(argv)
    if not a.llama_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        print("vision_hub: --llama-url must be loopback", file=sys.stderr)
        return 2
    svc = Service(LlamaClient.from_url(a.llama_url, a.timeout),
                  allowed_dir=Path(a.allowed_dir) if a.allowed_dir else None,
                  source=make_source(a.frames) if a.frames else None)
    uvicorn.run(create_app(svc), host="127.0.0.1", port=a.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
