"""CLI:  python -m device_hub --config hub.json        (real broker, TLS)
        python -m device_hub --mock                  (in-memory broker + fake gateway + two demo nodes, no network)
        python -m device_hub --mock --mcp-stdio      (MCP over stdio instead of HTTP, for local tool testing)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config
from .util import install_log_redaction


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="device_hub")
    ap.add_argument("--config", help="JSON config file")
    ap.add_argument("--mock", action="store_true", help="in-memory broker and fake gateway; no network except 127.0.0.1")
    ap.add_argument("--no-seed", action="store_true", help="with --mock: start empty instead of two demo nodes")
    ap.add_argument("--mcp-stdio", action="store_true", help="serve MCP on stdio instead of the HTTP API")
    ap.add_argument("--port", type=int, help="override http.port")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    install_log_redaction()
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("device_hub")

    cfg = load_config(args.config)
    if args.port:
        cfg.http.port = args.port

    fake_gw = None
    if args.mock:
        from .mock import build_mock_hub
        hub, _broker, fake_gw, _nodes = build_mock_hub(cfg.db_path if args.config else ":memory:", cfg.http.port,
                                                       seed=not args.no_seed)
        log.info("MOCK mode: in-memory broker, fake gateway, %d demo nodes", len(_nodes))
    else:
        from .hub import Hub
        hub = Hub.from_config(cfg)
        hub.start()

    if args.mcp_stdio:
        from mcp.server.stdio import stdio_server
        from .mcp_server import build_mcp_server
        server = build_mcp_server(hub.executor, hub.scenes)

        async def run_stdio() -> None:
            async with stdio_server() as (r, w):
                await server.run(r, w, server.create_initialization_options())

        asyncio.run(run_stdio())
        return 0

    import uvicorn
    from .api import create_app
    app = create_app(hub, mock_gateway=fake_gw)
    log.info("device-hub listening on http://%s:%d (MCP at /mcp)", cfg.http.host, cfg.http.port)
    try:
        uvicorn.run(app, host=cfg.http.host, port=cfg.http.port, log_config=None, access_log=False)
    finally:
        hub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
