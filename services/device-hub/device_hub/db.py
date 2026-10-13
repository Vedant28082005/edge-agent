"""SQLite (WAL) storage. One shared connection guarded by an RLock; every public Registry method takes the lock."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS nodes (
    node_id        TEXT PRIMARY KEY,
    hw             TEXT NOT NULL DEFAULT '',
    fw             TEXT NOT NULL DEFAULT '',
    failsafe_after_s INTEGER NOT NULL DEFAULT 60,
    announce_json  TEXT,                       -- last valid announce (normalised), source of pending endpoints
    announce_hash  TEXT,
    provisioned    INTEGER NOT NULL DEFAULT 1, -- 0 after unprovision (MQTT user removed)
    deleted        INTEGER NOT NULL DEFAULT 0,
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL,
    revocation_pending INTEGER NOT NULL DEFAULT 0  -- forget with force while dynsec was unreachable
);

CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id TEXT PRIMARY KEY,
    node_id     TEXT NOT NULL REFERENCES nodes(node_id),
    local_id    TEXT NOT NULL,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    room        TEXT NOT NULL,
    aliases     TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',   -- admin-set labels, never from an announce
    risk        TEXT NOT NULL DEFAULT 'normal' CHECK (risk IN ('normal','confirm')),
    enabled     INTEGER NOT NULL DEFAULT 1,
    present     INTEGER NOT NULL DEFAULT 1,    -- 0 when the bound node no longer announces this local_id
    deleted     INTEGER NOT NULL DEFAULT 0,
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_endpoint_binding ON endpoints(node_id, local_id) WHERE deleted = 0;

CREATE TABLE IF NOT EXISTS capabilities (
    endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    cap_id      TEXT NOT NULL,
    cap_json    TEXT NOT NULL,                 -- normalised capability object
    PRIMARY KEY (endpoint_id, cap_id)
);

CREATE TABLE IF NOT EXISTS rooms (name TEXT PRIMARY KEY);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    broken     INTEGER NOT NULL DEFAULT 0,
    broken_reasons TEXT NOT NULL DEFAULT '[]',
    deleted    INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    updated    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scene_name ON scenes(name COLLATE NOCASE) WHERE deleted = 0;

CREATE TABLE IF NOT EXISTS scene_actions (
    scene_id   TEXT NOT NULL REFERENCES scenes(scene_id),
    idx        INTEGER NOT NULL,
    target_json TEXT NOT NULL,
    capability TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (scene_id, idx)
);

CREATE TABLE IF NOT EXISTS provision_sessions (
    session_id TEXT PRIMARY KEY,
    node_id    TEXT NOT NULL,
    mqtt_username TEXT NOT NULL,               -- the password is never stored
    state      TEXT NOT NULL CHECK (state IN ('pending','connected','expired','cancelled')),
    created_by TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    expires_at REAL NOT NULL,
    connected_at REAL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '{}',        -- JSON; never contains secrets or tokens
    result  TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(endpoints)")}
            if "tags" not in cols:      # v1 database: add the column
                self.conn.execute("ALTER TABLE endpoints ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
            self.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('registry_version', '0')")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised transaction (BEGIN IMMEDIATE ... COMMIT/ROLLBACK). Re-entrant: inner use joins the outer."""
        with self.lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self.lock:
            self.conn.close()
