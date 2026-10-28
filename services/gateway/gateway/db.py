"""SQLite (WAL) store. One connection, autocommit, guarded by a lock (the workload is tiny)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','member','guest')), disabled INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_rooms(user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, room TEXT NOT NULL,
  PRIMARY KEY(user_id, room));
CREATE TABLE IF NOT EXISTS refresh_tokens(
  token_hash TEXT PRIMARY KEY, family_id TEXT NOT NULL, user_id TEXT NOT NULL, expires_at INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS refresh_family ON refresh_tokens(family_id);
CREATE TABLE IF NOT EXISTS confirm_requests(
  request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, action_json TEXT NOT NULL, action_hash TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '', session_id TEXT, status TEXT NOT NULL, created INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  retry_of TEXT);
CREATE TABLE IF NOT EXISTS confirm_tokens(
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, action_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, request_id TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT 'ok');
CREATE TABLE IF NOT EXISTS wifi_profiles(name TEXT PRIMARY KEY, ssid TEXT NOT NULL, password_enc BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS scene_assignments(user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, scene_id TEXT NOT NULL,
  PRIMARY KEY(user_id, scene_id));
CREATE TABLE IF NOT EXISTS action_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, user_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
  node_id TEXT, endpoint_id TEXT, scene_id TEXT, capability TEXT, value_json TEXT, status TEXT NOT NULL, error TEXT,
  call_id TEXT, elapsed_ms INTEGER);
CREATE INDEX IF NOT EXISTS action_log_ts ON action_log(ts);
CREATE INDEX IF NOT EXISTS action_log_ep ON action_log(endpoint_id, capability, status);
CREATE TABLE IF NOT EXISTS automation_rules(
  rule_id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','approved','disabled')), created_by TEXT NOT NULL CHECK(created_by IN ('user','llm')),
  body TEXT NOT NULL, cooldown_s INTEGER NOT NULL, max_runs_per_day INTEGER NOT NULL, approved_by TEXT, approved_at INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, broken INTEGER NOT NULL DEFAULT 0, rooms TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS automation_runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, ts INTEGER NOT NULL, outcome TEXT NOT NULL, action_status TEXT,
  action_log_id INTEGER, call_id TEXT, error TEXT, rule_deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS automation_runs_rule ON automation_runs(rule_id, run_id);
CREATE TABLE IF NOT EXISTS automation_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, notification_id TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL, level TEXT NOT NULL,
  source TEXT NOT NULL, rule_id TEXT, endpoint_id TEXT, text TEXT NOT NULL, owner TEXT, rooms TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS notification_reads(user_id TEXT NOT NULL, notification_id TEXT NOT NULL, PRIMARY KEY(user_id, notification_id));
"""


class DB:
    def __init__(self, path: str, clock=time.time) -> None:
        self.clock = clock
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._c.execute("PRAGMA journal_mode=WAL")
            self._c.execute("PRAGMA foreign_keys=ON")
            self._c.execute("PRAGMA synchronous=NORMAL")
            self._c.executescript(SCHEMA)

    def now(self) -> int:
        return int(self.clock())

    def run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._c.execute(sql, args)

    def one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._c.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._c.execute(sql, args).fetchall()

    def tx(self):
        """BEGIN IMMEDIATE ... COMMIT as a context manager."""
        db = self

        class _Tx:
            def __enter__(self_):
                db._lock.acquire()
                db._c.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_, et, ev, tb):
                try:
                    db._c.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db._lock.release()
        return _Tx()

    def audit(self, actor: str, action: str, target: str = "", detail: dict | None = None, result: str = "ok") -> None:
        """Audit rows never carry secrets: callers pass only ids/names/reasons."""
        self.run("INSERT INTO audit_log(ts,actor,action,target,detail,result) VALUES(?,?,?,?,?,?)",
                 (self.now(), actor, action, target, json.dumps(detail or {}, sort_keys=True), result))

    def close(self) -> None:
        with self._lock:
            self._c.close()
