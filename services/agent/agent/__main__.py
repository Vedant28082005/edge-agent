"""CLI:  python -m agent --mock                      scripted LLM + fake hub, no network (127.0.0.1 only)
        python -m agent --mock --insecure-no-auth    same, without the gateway secret (mock only)
        python -m agent --config agent.json          real hub + llama-server
The gateway-to-agent secret is verified against a SHA-256 verifier file (config `api_verifier_file`); the agent refuses
to start without it. `--insecure-no-auth` is accepted only together with `--mock`.
Flags: --constrained on|off  (runtime default; also switchable via POST /v1/runtime)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
import tempfile

from .auth import ApiAuth
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
    hub_server = None
    auth = None
    if args.mock:
        tmp = tempfile.mkdtemp(prefix="agent-mock-")
        hub_secret = secrets.token_urlsafe(24)           # generated per run, written to a temp file, never printed
        cfg.hub_secret_file = os.path.join(tmp, "agent.secret")
        with open(cfg.hub_secret_file, "w", encoding="utf-8") as f:
            f.write(hub_secret)
        cfg.db_path = ":memory:" if cfg.db_path == "agent.sqlite" else cfg.db_path
        fake = FakeHub(hub_secret)
        hub_server = LocalServer(fake.app())
        cfg.hub_url = await hub_server.start()
        llm = ScriptedLLM(cfg.constrained)
        if not args.insecure_no_auth:
            api_secret = secrets.token_urlsafe(24)
            secret_path = os.path.join(tmp, "agent-api.secret")
            with open(secret_path, "w", encoding="utf-8") as f:
                f.write(api_secret)
            auth = ApiAuth.from_secret(api_secret, os.path.join(tmp, "agent-api.sha256"))
            logging.getLogger("agent").info("mock gateway secret is in %s (send it as X-Service-Secret)", secret_path)
    else:
        auth = ApiAuth(cfg.api_verifier_file)             # SystemExit if missing: no unauthenticated real mode
        llm = LlmClient(cfg.llm_url, cfg.llm_model, cfg.constrained, cfg.llm_max_tokens, cfg.llm_timeout_s)
    agent = Agent.from_config(cfg, llm)
    agent.mock = args.mock
    await agent.start()
    if auth is None:
        logging.getLogger("agent").warning("INSECURE: gateway authentication is OFF (mock mode only)")
    app = create_app(agent, auth)
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
    ap.add_argument("--insecure-no-auth", action="store_true", help="no gateway authentication; accepted only with --mock")
    ap.add_argument("--constrained", choices=("on", "off"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    if args.insecure_no_auth and not args.mock:
        ap.error("--insecure-no-auth is accepted only together with --mock")
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
