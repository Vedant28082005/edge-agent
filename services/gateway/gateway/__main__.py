"""CLI.

  python -m gateway serve --config gateway.json      public 127.0.0.1:8000 + internal 127.0.0.1:8001 (or unix socket)
  python -m gateway --mock                           fakes in-process, no upstream network; prints demo logins to stderr
  python -m gateway bootstrap-admin --username NAME  one-time first admin; password prompted (or --password-stdin); no default
  python -m gateway wifi-profile add|list|remove     saved Wi-Fi profiles (password prompted, stored encrypted)
  python -m gateway assign-scene USER SCENE_ID       let a guest run a scene (or --remove)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from .config import load_config
from .security import SECRETS, hash_password, install_log_redaction


def _read_password(args, prompt="Password: ") -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    a, b = getpass.getpass(prompt), getpass.getpass("Repeat: ")
    if a != b:
        raise SystemExit("passwords do not match")
    return a


def _offline_ctx(args):
    from .context import build_ctx
    cfg = load_config(args.config)
    return build_ctx(cfg, hub_secret="unused-offline-cli")


async def _serve(ctx, cfg, app_pub, app_int) -> None:
    import uvicorn
    ctx.start()
    common = dict(log_config=None, access_log=False, server_header=False)
    pub = uvicorn.Server(uvicorn.Config(app_pub, host=cfg.public.host, port=cfg.public.port, **common))
    if cfg.internal.uds:
        icfg = uvicorn.Config(app_int, uds=cfg.internal.uds, **common)
    else:
        icfg = uvicorn.Config(app_int, host=cfg.internal.host, port=cfg.internal.port, **common)
    internal = uvicorn.Server(icfg)
    try:
        await asyncio.gather(pub.serve(), internal.serve())
    finally:
        await ctx.shutdown()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gateway")
    ap.add_argument("--config")
    ap.add_argument("--mock", action="store_true", help="in-process fake hub/agent/vision/yolo; no upstream network")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    b = sub.add_parser("bootstrap-admin")
    b.add_argument("--username", required=True)
    b.add_argument("--password-stdin", action="store_true")
    w = sub.add_parser("wifi-profile")
    w.add_argument("action", choices=["add", "list", "remove"])
    w.add_argument("--name")
    w.add_argument("--ssid")
    w.add_argument("--password-stdin", action="store_true")
    a = sub.add_parser("assign-scene")
    a.add_argument("username")
    a.add_argument("scene_id")
    a.add_argument("--remove", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    for n in ("httpx", "httpcore"):
        logging.getLogger(n).setLevel(logging.WARNING)

    if args.mock:
        from .app import create_public_app
        from .internal import create_internal_app
        from .mock import build_mock
        cfg = load_config(args.config)
        m = build_mock(cfg)
        print("MOCK mode (data in a temp dir). Demo logins, printed once:", file=sys.stderr)
        for u, p in m.passwords.items():
            print(f"  {u}: {p}", file=sys.stderr)
        asyncio.run(_serve(m.ctx, m.ctx.cfg, create_public_app(m.ctx), m.internal_app))
        return 0

    if args.cmd == "bootstrap-admin":
        ctx = _offline_ctx(args)
        if ctx.auth.admin_count() or ctx.db.one("SELECT 1 FROM users LIMIT 1"):
            raise SystemExit("users already exist: bootstrap is a one-time command (use /admin/users)")
        pw = _read_password(args)
        if len(pw) < 12:
            raise SystemExit("password must be at least 12 characters")
        u = ctx.auth.create(args.username, hash_password(pw), "admin")
        ctx.db.audit("cli", "bootstrap_admin", u.user_id)
        print(f"admin '{u.username}' created")
        return 0
    if args.cmd == "wifi-profile":
        ctx = _offline_ctx(args)
        if args.action == "list":
            print("\n".join(ctx.profiles.names()))
        elif args.action == "remove":
            print("removed" if ctx.profiles.delete(args.name) else "no such profile")
        else:
            if not (args.name and args.ssid):
                raise SystemExit("--name and --ssid are required")
            pw = _read_password(args, "Wi-Fi password: ")
            SECRETS.add(pw)
            ctx.profiles.put(args.name, args.ssid, pw)
            ctx.db.audit("cli", "wifi_profile_put", args.name)
            print("saved")
        return 0
    if args.cmd == "assign-scene":
        ctx = _offline_ctx(args)
        row = ctx.db.one("SELECT user_id, role FROM users WHERE username=?", (args.username,))
        if row is None:
            raise SystemExit("no such user")
        if args.remove:
            ctx.db.run("DELETE FROM scene_assignments WHERE user_id=? AND scene_id=?", (row["user_id"], args.scene_id))
        else:
            ctx.db.run("INSERT OR IGNORE INTO scene_assignments(user_id,scene_id) VALUES(?,?)", (row["user_id"], args.scene_id))
        ctx.db.audit("cli", "assign_scene", args.scene_id, {"user": row["user_id"], "remove": args.remove})
        print("ok")
        return 0
    if args.cmd in (None, "serve"):
        from .app import create_public_app
        from .context import build_ctx
        from .internal import create_internal_app
        cfg = load_config(args.config)
        ctx = build_ctx(cfg)
        asyncio.run(_serve(ctx, cfg, create_public_app(ctx), create_internal_app(ctx)))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
