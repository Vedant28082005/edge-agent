"""Device action log (gateway.yaml `GET /admin/action-log`): one row per action, updated in place as it settles.

Holds only ids, enum values and error codes. Never chat text, prompts, tool arguments beyond the action itself, tokens or
agent turn traces (those are a different store). Values written here are the action's own scalar `value`."""
from __future__ import annotations

import base64
import binascii
import json
import re

from .db import DB

STATUSES = ("done", "moving", "settled", "settle_timeout", "failed", "blocked", "confirmation_required", "declined", "expired",
            "denied")
SOURCES = ("direct", "agent", "scene", "automation", "admin")
NODE_RE = re.compile(r"^esp32-[0-9a-f]{12}$")
EP_RE = re.compile(r"^ep_[0-9a-f]{8}$")
CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
SCALAR = (bool, int, float, str)
COLS = ("id", "ts", "user_id", "source", "kind", "node_id", "endpoint_id", "scene_id", "capability", "value", "status", "error",
        "call_id", "elapsed_ms")


def _code(x) -> str | None:
    """Error codes only (mcp-tools.md style); anything else (free text) is not stored."""
    return x if isinstance(x, str) and CODE_RE.match(x) else None


def _scalar(v):
    return v if isinstance(v, SCALAR) and not (isinstance(v, float) and v != v) else None


def _vj(v) -> str | None:
    """JSON text of a scalar value, integral floats as ints (so 1.0 and 1 match)."""
    v = _scalar(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return None if v is None else json.dumps(v)


class ActionLog:
    def __init__(self, db: DB) -> None:
        self.db = db

    def record(self, user_id: str, source: str, kind: str, status: str, *, node_id=None, endpoint_id=None, scene_id=None,
               capability=None, value=None, error=None, call_id=None, elapsed_ms=None) -> int:
        assert source in SOURCES and kind in ("set_state", "run_scene") and status in STATUSES
        node_id = node_id if isinstance(node_id, str) and NODE_RE.match(node_id) else None
        endpoint_id = endpoint_id if isinstance(endpoint_id, str) and EP_RE.match(endpoint_id) else None
        cur = self.db.run(
            "INSERT INTO action_log(ts,user_id,source,kind,node_id,endpoint_id,scene_id,capability,value_json,status,error,call_id,"
            "elapsed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.db.now(), str(user_id)[:64], source, kind, node_id, endpoint_id,
             scene_id[:64] if isinstance(scene_id, str) else None,
             capability[:64] if isinstance(capability, str) else None,
             _vj(value), status, _code(error),
             call_id[:64] if isinstance(call_id, str) and call_id else None,
             elapsed_ms if isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool) and elapsed_ms >= 0 else None))
        return cur.lastrowid

    def settle(self, ok: bool, endpoint_id, capability, value, call_id, elapsed_ms=None) -> bool:
        """A settle event: the matching `moving` row becomes settled / settle_timeout (by call_id, else the newest moving row
        for that endpoint + capability + value). Idempotent: a row that is no longer `moving` is left alone."""
        if not (isinstance(endpoint_id, str) and isinstance(capability, str)):
            return False
        status = "settled" if ok else "settle_timeout"
        ms = elapsed_ms if isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool) and elapsed_ms >= 0 else None
        row = None
        if isinstance(call_id, str) and call_id:
            row = self.db.one("SELECT id FROM action_log WHERE call_id=? AND endpoint_id=? AND capability=? AND status='moving' "
                              "ORDER BY id DESC LIMIT 1", (call_id, endpoint_id, capability))
        if row is None:
            row = self.db.one("SELECT id FROM action_log WHERE endpoint_id=? AND capability=? AND value_json=? AND status='moving' "
                              "AND (call_id IS NULL OR call_id=?) ORDER BY id DESC LIMIT 1",
                              (endpoint_id, capability, _vj(value), call_id if isinstance(call_id, str) else ""))
        if row is None:
            return False
        return bool(self.db.run("UPDATE action_log SET status=?, elapsed_ms=COALESCE(?,elapsed_ms) WHERE id=? AND status='moving'",
                                (status, ms, row["id"])).rowcount)

    # ------------------------------------------------------------------ read
    @staticmethod
    def encode_cursor(last_id: int) -> str:
        return base64.urlsafe_b64encode(f"a1:{last_id}".encode()).decode().rstrip("=")

    @staticmethod
    def decode_cursor(c: str) -> int | None:
        try:
            raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        m = re.fullmatch(r"a1:([0-9]{1,15})", raw)
        return int(m.group(1)) if m else None

    @staticmethod
    def _entry(r) -> dict:
        return {"id": r["id"], "ts": r["ts"], "user_id": r["user_id"], "source": r["source"], "kind": r["kind"],
                "node_id": r["node_id"], "endpoint_id": r["endpoint_id"], "scene_id": r["scene_id"],
                "capability": r["capability"], "value": json.loads(r["value_json"]) if r["value_json"] is not None else None,
                "status": r["status"], "error": r["error"], "call_id": r["call_id"], "elapsed_ms": r["elapsed_ms"]}

    def page(self, *, node_id=None, endpoint_id=None, user_id=None, status=None, ts_from=None, ts_to=None, limit=50,
             after_id: int | None = None) -> dict:
        """Newest first by id (stable under inserts); `next_cursor` is None on the last page."""
        where, args = [], []
        for col, v in (("node_id", node_id), ("endpoint_id", endpoint_id), ("user_id", user_id), ("status", status)):
            if v is not None:
                where.append(f"{col}=?")
                args.append(v)
        if ts_from is not None:
            where.append("ts>=?")
            args.append(ts_from)
        if ts_to is not None:
            where.append("ts<?")
            args.append(ts_to)
        if after_id is not None:
            where.append("id<?")
            args.append(after_id)
        sql = "SELECT * FROM action_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.all(sql, (*args, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [self._entry(r) for r in rows], "next_cursor": self.encode_cursor(rows[-1]["id"]) if more else None}
