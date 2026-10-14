"""CLI:  python -m agent --mock                 scripted LLM + fake hub, no network (127.0.0.1 only)
        python -m agent --config agent.json     real hub + llama-server
Flags: --constrained on|off  (runtime default; also switchable via POST /v1/runtime)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
import tempfile

from .config import load_config
from .util import install_log_redaction


async def amain(args) -> int:
    import uvicorn

    from .app import create_app
    from .llm import LlmClient
    from .mock import FakeHub, LocalServer, ScriptedLLM
    from .service import Agent

    cfg = load_config(args.config)
    if args.constrained:
        cfg.constrained = args.constrained == "on"
    if args.port:
        cfg.port = args.port
    fake = hub_server = None
    tmp = None
    if args.mock:
        secret = secrets.token_urlsafe(24)             # generated per run, written to a temp file, never printed
        tmp = tempfile.mkdtemp(prefix="agent-mock-")
        cfg.hub_secret_file = os.path.join(tmp, "agent.secret")
        with open(cfg.hub_secret_file, "w", encoding="utf-8") as f:
            f.write(secret)
        cfg.db_path = ":memory:" if cfg.db_path == "agent.sqlite" else cfg.db_path
        fake = FakeHub(secret)
        hub_server = LocalServer(fake.app())
        cfg.hub_url = await hub_server.start()
        llm = ScriptedLLM(cfg.constrained)
    else:
        llm = LlmClient(cfg.llm_url, cfg.llm_model, cfg.constrained, cfg.llm_max_tokens, cfg.llm_timeout_s)
    agent = Agent.from_config(cfg, llm)
    await agent.start()

    mock_confirm = None
    if args.mock:
        def mock_confirm(request_id: str, user_id: str, approve: bool) -> bool:    # stands in for the gateway
            action = agent.confirmations.pending_action(request_id)
            if not approve or action is None:
                return agent.confirmations.resolve(request_id, user_id, False)
            return agent.confirmations.resolve(request_id, user_id, True, fake.mint(user_id, action))

    app = create_app(agent, mock_confirm)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="info", access_log=False))
    logging.getLogger("agent").info("agent listening on http://%s:%d (%s)", cfg.host, cfg.port, "MOCK" if args.mock else "real")
    try:
        await server.serve()
    finally:
        await agent.stop()
        if hub_server:
            await hub_server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent")
    ap.add_argument("--config")
    ap.add_argument("--mock", action="store_true", help="scripted LLM and fake hub, no network")
    ap.add_argument("--constrained", choices=("on", "off"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
