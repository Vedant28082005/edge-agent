"""SQLite persistence: per-user messages and a rolling summary, the action log, turn traces (own table and retention cap), and the (empty) rules table.

Per-user isolation: every read/write of conversation data takes `user_id` and filters on it; there is no query that
returns another user's rows. Guests have no persistent memory: callers pass `guest=True` (the `role` the authenticated
gateway sent; the store never infers it from a user id) and then messages live in a small in-process LRU only and
memories do not exist. The action log is an audit trail and is kept for everyone.
No token, secret or `_meta` ever reaches this module (callers pass validated tool arguments and result summaries)."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')), text TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id, session_id, id);
CREATE TABLE IF NOT EXISTS summaries(
  user_id TEXT NOT NULL, session_id TEXT NOT NULL, text TEXT NOT NULL, upto_id INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL,
  PRIMARY KEY(user_id, session_id));
CREATE TABLE IF NOT EXISTS action_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT, req_id TEXT, trigger_kind TEXT NOT NULL,
  trigger_ref TEXT, reason TEXT, tool TEXT, args TEXT, status TEXT NOT NULL, ok INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS action_log_ts ON action_log(ts);
CREATE TABLE IF NOT EXISTS turn_traces(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT, req_id TEXT, route TEXT, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, event_type TEXT NOT NULL, when_json TEXT NOT NULL DEFAULT '{}',
  actions_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS memories(
  memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, text TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS memories_user ON memories(user_id, updated_at);
"""


class Store:
    def __init__(self, path: str = ":memory:", guest_cache: int = 64, trace_cap: int = 5000) -> None:
        self.trace_cap = max(1, trace_cap)               # turn_traces retention: newest N rows, independent of action_log
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._guest: OrderedDict[tuple[str, str], list[tuple[str, str]]] = OrderedDict()
        self._guest_cache = guest_cache
        # Pending clarification per (user_id, session_id): the ORIGINAL intent (ambient domain+direction, so far)
        # behind a clarifying question just asked, so a short "yes"-shaped follow-up can resolve it without
        # re-deriving anything from that follow-up's own (near-empty) text. Chat state, not conversation history:
        # never persisted, and cleared the moment it is read (one shot, like the question itself).
        self._pending: dict[tuple[str, str], dict] = {}

    def close(self) -> None:
        self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------------------------------ conversation
    def add_message(self, user_id: str, session_id: str, role: str, text: str, guest: bool = False) -> None:
        if guest:
            key = (user_id, session_id)
            h = self._guest.setdefault(key, [])
            h.append((role, text))
            del h[:-8]
            self._guest.move_to_end(key)
            while len(self._guest) > self._guest_cache:
                self._guest.popitem(last=False)
            return
        self._q("INSERT INTO messages(user_id,session_id,role,text,ts) VALUES(?,?,?,?,?)",
                (user_id, session_id, role, text, time.time()))

    def recent(self, user_id: str, session_id: str, n: int = 4, guest: bool = False) -> list[tuple[str, str]]:
        if guest:
            return list(self._guest.get((user_id, session_id), []))[-n:]
        rows = self._q("SELECT role,text FROM messages WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
                       (user_id, session_id, n))
        return [(r["role"], r["text"]) for r in reversed(rows)]

    def recent_rows(self, user_id: str, session_id: str, n: int = 16, guest: bool = False) -> list[tuple[int, str, str]]:
        """(id, role, text) of this user's last n messages, oldest first. Guests: in-process history, synthetic ids."""
        if guest:
            return [(i, r, t) for i, (r, t) in enumerate(list(self._guest.get((user_id, session_id), []))[-n:])]
        rows = self._q("SELECT id,role,text FROM messages WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
                       (user_id, session_id, n))
        return [(r["id"], r["role"], r["text"]) for r in reversed(rows)]

    def summary_upto(self, user_id: str, session_id: str) -> int:
        rows = self._q("SELECT upto_id FROM summaries WHERE user_id=? AND session_id=?", (user_id, session_id))
        return rows[0]["upto_id"] if rows else 0

    def put_summary(self, user_id: str, session_id: str, text: str, upto_id: int) -> None:
        self._q("INSERT INTO summaries(user_id,session_id,text,upto_id,ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,session_id) DO UPDATE SET text=excluded.text, upto_id=excluded.upto_id, ts=excluded.ts",
                (user_id, session_id, text, upto_id, time.time()))

    def summary(self, user_id: str, session_id: str, guest: bool = False) -> str:
        if guest:
            return ""
        rows = self._q("SELECT text FROM summaries WHERE user_id=? AND session_id=?", (user_id, session_id))
        return rows[0]["text"] if rows else ""

    def roll_summary(self, user_id: str, session_id: str, keep: int = 4, max_chars: int = 400, guest: bool = False) -> bool:
        """Fold messages older than the last `keep` into the rolling summary (deterministic, no LLM: the LLM-written
        summary is S8's idle job). Returns True if something was folded."""
        if guest:
            return False
        rows = self._q("SELECT id FROM messages WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                       (user_id, session_id, keep - 1))
        if not rows:
            return False
        cutoff = rows[0]["id"]
        done = self._q("SELECT upto_id FROM summaries WHERE user_id=? AND session_id=?", (user_id, session_id))
        upto = done[0]["upto_id"] if done else 0
        old = self._q("SELECT role,text FROM messages WHERE user_id=? AND session_id=? AND id>? AND id<? ORDER BY id",
                      (user_id, session_id, upto, cutoff))
        if not old:
            return False
        add = " ".join(("U: " if r["role"] == "user" else "A: ") + r["text"][:80] for r in old)
        text = ((done and self.summary(user_id, session_id) or "") + " " + add).strip()[-max_chars:]
        self._q("INSERT INTO summaries(user_id,session_id,text,upto_id,ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,session_id) DO UPDATE SET text=excluded.text, upto_id=excluded.upto_id, ts=excluded.ts",
                (user_id, session_id, text, cutoff - 1, time.time()))
        return True

    def forget_user(self, user_id: str) -> None:
        for t in ("messages", "summaries", "memories"):
            self._q(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
        for key in [k for k in self._pending if k[0] == user_id]:
            del self._pending[key]

    # ------------------------------------------------------------------------------------------ pending clarification
    def set_pending(self, user_id: str, session_id: str, intent: dict) -> None:
        self._pending[(user_id, session_id)] = intent

    def pop_pending(self, user_id: str, session_id: str) -> dict | None:
        return self._pending.pop((user_id, session_id), None)

    # ------------------------------------------------------------------------------------------ memories (/v1/memory)
    def add_memory(self, user_id: str, text: str, guest: bool = False) -> dict | None:
        if guest:
            return None
        mid = "m_" + secrets.token_hex(6)
        self._q("INSERT INTO memories(memory_id,user_id,text,updated_at) VALUES(?,?,?,?)", (mid, user_id, text[:500], int(time.time())))
        return {"memory_id": mid, "text": text[:500], "updated_at": int(time.time())}

    def list_memories(self, user_id: str, guest: bool = False) -> list[dict]:
        if guest:
            return []
        return [{"memory_id": r["memory_id"], "text": r["text"], "updated_at": r["updated_at"]} for r in
                self._q("SELECT * FROM memories WHERE user_id=? ORDER BY updated_at DESC, rowid DESC", (user_id,))]

    def update_memory(self, user_id: str, memory_id: str, text: str) -> dict | None:
        """None if the memory does not exist FOR THIS USER (another user's memory looks the same: no existence leak)."""
        now = int(time.time())
        with self._lock:
            cur = self._db.execute("UPDATE memories SET text=?, updated_at=? WHERE memory_id=? AND user_id=?",
                                   (text[:500], now, memory_id, user_id))
        return {"memory_id": memory_id, "text": text[:500], "updated_at": now} if cur.rowcount else None

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM memories WHERE memory_id=? AND user_id=?", (memory_id, user_id)).rowcount > 0

    # ------------------------------------------------------------------------------------------ action log
    def log_action(self, *, user_id: str | None, req_id: str | None, trigger_kind: str, trigger_ref: str | None,
                   reason: str, tool: str | None, args: dict | None, status: str, ok: bool | None,
                   detail: dict | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO action_log(ts,user_id,req_id,trigger_kind,trigger_ref,reason,tool,args,status,ok,detail) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), user_id, req_id, trigger_kind, trigger_ref, reason[:200], tool,
                 json.dumps(args, sort_keys=True) if args is not None else None, status,
                 None if ok is None else int(ok), json.dumps(detail, sort_keys=True) if detail else None))
            return cur.lastrowid or 0

    def action_log(self, limit: int = 50, user_id: str | None = None) -> list[dict]:
        """Audit rows only. Turn traces live in `turn_traces`; the status filter also hides any legacy `trace` rows."""
        if user_id is None:
            rows = self._q("SELECT * FROM action_log WHERE status<>'trace' ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._q("SELECT * FROM action_log WHERE status<>'trace' AND user_id=? ORDER BY id DESC LIMIT ?",
                           (user_id, limit))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------------------------------ turn traces (S10)
    def log_trace(self, *, user_id: str | None, req_id: str | None, route: str, detail: dict) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO turn_traces(ts,user_id,req_id,route,detail) VALUES(?,?,?,?,?)",
                                   (time.time(), user_id, req_id, route, json.dumps(detail, sort_keys=True)))
            rid = cur.lastrowid or 0
            self._db.execute("DELETE FROM turn_traces WHERE id<=?", (rid - self.trace_cap,))
            return rid

    def traces(self, limit: int = 50, user_id: str | None = None) -> list[dict]:
        """Newest first; `detail` is the decoded trace dict."""
        if user_id is None:
            rows = self._q("SELECT * FROM turn_traces ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._q("SELECT * FROM turn_traces WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ------------------------------------------------------------------------------------------ rules (empty in v1)
    def enabled_rules(self) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM rules WHERE enabled=1 ORDER BY id"):
            out.append({"id": r["id"], "name": r["name"], "event_type": r["event_type"],
                        "when": json.loads(r["when_json"]), "actions": json.loads(r["actions_json"])})
        return out

    def add_rule(self, name: str, event_type: str, when: dict, actions: list[dict]) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO rules(name,event_type,when_json,actions_json) VALUES(?,?,?,?)",
                                   (name, event_type, json.dumps(when), json.dumps(actions)))
            return cur.lastrowid or 0
