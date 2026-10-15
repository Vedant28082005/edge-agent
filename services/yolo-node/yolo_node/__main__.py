"""CLI:  python -m yolo_node --config yolo-node.json
       python -m yolo_node --mock --verifier-file V        mock detector + mock camera, authenticated
       python -m yolo_node --mock --insecure-no-auth       same without the caller secret (mock only, warns on start)
The caller secret is verified against a SHA-256 verifier file; the node refuses to start without it."""
from __future__ import annotations

import argparse
import logging
import sys

from .app import Node, create_app
from .auth import ApiAuth
from .backends import make_backend
from .config import Config, load_config, validate
from .sources import build_sources


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="yolo_node")
    ap.add_argument("--config")
    ap.add_argument("--mock", action="store_true", help="mock backend and a synthetic camera; builds and tests on a PC")
    ap.add_argument("--insecure-no-auth", action="store_true", help="only together with --mock")
    ap.add_argument("--verifier-file")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-camera", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.insecure_no_auth and not args.mock:
        print("--insecure-no-auth is accepted only together with --mock", file=sys.stderr)
        return 2
    cfg = load_config(args.config) if args.config else Config()
    if args.mock:
        cfg.backend = "mock"
        if not args.config:
            cfg.sources = [{"source_id": "cam0", "type": "usb"}]
    if args.no_camera:
        cfg.sources = []
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.verifier_file:
        cfg.verifier_file = args.verifier_file
    validate(cfg)

    if args.insecure_no_auth:
        auth = None
        logging.getLogger("yolo_node").warning("RUNNING WITHOUT CALLER AUTHENTICATION (--insecure-no-auth, mock only)")
    else:
        auth = ApiAuth(cfg.verifier_file)            # SystemExit if missing: no unauthenticated real mode

    node = Node(cfg, make_backend(cfg), build_sources(cfg.sources, args.mock), auth)
    import uvicorn
    uvicorn.run(create_app(node), host=cfg.host, port=cfg.port, log_level="warning", access_log=False,
                server_header=False, timeout_keep_alive=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
