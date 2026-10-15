"""CLI:  python -m vision_node --config vision-node.json        (real: llama-server + whisper.cpp on loopback, ONNX YOLO)
        python -m vision_node --mock --insecure-no-auth          (deterministic fake models, no network except 127.0.0.1)
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vision_node")
    ap.add_argument("--config", help="JSON config file (non-secret)")
    ap.add_argument("--mock", action="store_true", help="mock backends; nothing is loaded or contacted")
    ap.add_argument("--insecure-no-auth", action="store_true",
                    help="no caller authentication; accepted only with --mock (a warning is logged on every start)")
    ap.add_argument("--host", help="override the bind address (production: the Tailscale address)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    if args.insecure_no_auth and not args.mock:
        ap.error("--insecure-no-auth is accepted only together with --mock")

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("vision_node")

    cfg = load_config(args.config)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    cfg.validate(mock=args.mock)

    from .app import create_app
    from .auth import CallerAuth
    from .contract import Contract

    auth = None
    if args.insecure_no_auth:
        log.warning("INSECURE: caller authentication is disabled (--insecure-no-auth, mock only)")
    else:
        auth = CallerAuth(cfg.verifier_file)        # refuses to start without a verifier

    contract = Contract(cfg.contracts_dir)
    if args.mock:
        from .backends import mock_backends
        from .sysinfo import MockSysInfo
        app = create_app(cfg, mock_backends(), contract=contract, auth=auth, sysinfo=MockSysInfo(), mock=True)
        log.info("MOCK mode: deterministic fake models")
    else:
        from .backends import Backends
        from .real import LlamaVlm, OnnxYolo, WhisperStt
        be = Backends(LlamaVlm(cfg.llama_url, cfg.llama_model_name), WhisperStt(cfg.whisper_url, cfg.whisper_model_name),
                      OnnxYolo(cfg.yolo_model_path, cfg.yolo_model_name))
        app = create_app(cfg, be, contract=contract, auth=auth)

    import uvicorn
    log.info("vision-node (contract %s) listening on http://%s:%d", contract.version, cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None, access_log=False, timeout_keep_alive=5, server_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
