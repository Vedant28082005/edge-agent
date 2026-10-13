"""Registry: nodes, endpoints, capabilities, rooms, audit log. Owned by device-hub (plan section 4.5).

Names, rooms and aliases only ever come from admin calls (approve/patch), never from announces. Announce data is
stored normalised in nodes.announce_json; endpoints (with permanent ids) exist only after an admin approves them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import ENDPOINT_TYPES, Contracts, coerce_value, normalise_capability
from .db import Database
from .errors import Conflict, HubError, NotFound
from .events import EventBus
from .util import (ENDPOINT_ID_RE, NODE_ID_RE, Clock, canonical_json, new_endpoint_id, safe_repr, sanitize_text)

log = logging.getLogger("device_hub.registry")

NAME_MAX = 48
ALIASES_MAX = 8
TAGS_MAX = 8
TAG_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


@dataclass
class AnnounceResult:
    is_new: bool
    changed: bool
    pending_local_ids: list[str]


class LiveState:
    """In-memory live data fed by MQTT: node online flags and last reported endpoint values. Kept apart from the DB
    so message ordering at start-up (retained status/state before announce) never matters. A condition variable lets
    the executor wait for a FRESH state message (counter bump) after it sent a command."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._online: dict[str, bool] = {}
        self._values: dict[tuple[str, str], tuple[dict, int]] = {}
        self._counter: dict[tuple[str, str], int] = {}

    def set_online(self, node_id: str, online: bool) -> bool:
        with self._cond:
            changed = self._online.get(node_id) != online
            self._online[node_id] = online
            return changed

    def is_online(self, node_id: str) -> bool:
        with self._cond:
            return self._online.get(node_id, False)

    def update_values(self, node_id: str, local_id: str, values: dict, ts: int) -> bool:
        """Store a state message. Returns True when the values differ from before (state dedup for events); the
        freshness counter is bumped either way."""
        with self._cond:
            old = self._values.get((node_id, local_id))
            self._values[(node_id, local_id)] = (dict(values), ts)
            self._counter[(node_id, local_id)] = self._counter.get((node_id, local_id), 0) + 1
            self._cond.notify_all()
            return old is None or old[0] != values

    def counter(self, node_id: str, local_id: str) -> int:
        with self._cond:
            return self._counter.get((node_id, local_id), 0)

    def wait_for(self, node_id: str, local_id: str, after: int, pred, timeout: float) -> tuple[bool, dict | None]:
        """Wait until a state message newer than counter `after` satisfies pred(values). Returns (ok, values of the
        latest state message received AFTER `after`, or None if none arrived)."""
        key = (node_id, local_id)

        def ready() -> bool:
            if self._counter.get(key, 0) <= after:
                return False
            return pred(self._values[key][0])

        with self._cond:
            ok = self._cond.wait_for(ready, timeout)
            v = self._values.get(key)
            fresh = v is not None and self._counter.get(key, 0) > after
            return ok, (dict(v[0]) if fresh else None)

    def get_values(self, node_id: str, local_id: str) -> tuple[dict, int] | None:
        with self._cond:
            v = self._values.get((node_id, local_id))
            return (dict(v[0]), v[1]) if v else None

    def drop_node(self, node_id: str) -> None:
        with self._cond:
            self._online.pop(node_id, None)
            for k in [k for k in self._values if k[0] == node_id]:
                del self._values[k]
                self._counter.pop(k, None)


def clean_label(value: Any, field: str, required: bool = True) -> str:
    s = sanitize_text(value, NAME_MAX)
    if required and not s:
        raise HubError("bad_request", f"{field} must be a non-empty label")
    return s


def clean_aliases(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > ALIASES_MAX:
        raise HubError("bad_request", f"aliases must be a list of at most {ALIASES_MAX} strings")
    out: list[str] = []
    for a in value:
        if not isinstance(a, str):
            raise HubError("bad_request", "aliases must be strings")
        s = sanitize_text(a, NAME_MAX)
        if s and s.lower() not in [x.lower() for x in out]:
            out.append(s)
    return out


def clean_tags(value: Any) -> list[str]:
    """Admin-set tags: at most 8, each 1..24 of [a-z0-9_-], unique. Strict: invalid input is an error, not repaired."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > TAGS_MAX:
        raise HubError("bad_request", f"tags must be a list of at most {TAGS_MAX} strings")
    for t in value:
        if not isinstance(t, str) or not TAG_RE.match(t):
            raise HubError("bad_request", "each tag must be 1..24 characters of [a-z0-9_-]")
    if len(set(value)) != len(value):
        raise HubError("bad_request", "tags must be unique")
    return list(value)


class Registry:
    def __init__(self, db: Database, contracts: Contracts, bus: EventBus, clock: Clock) -> None:
        self.db = db
        self.contracts = contracts
        self.bus = bus
        self.clock = clock
        self.live = LiveState()
        self.link_up = lambda: True     # Hub points this at the broker connection: without it nothing is "online"
        self._hooks: list[Any] = []   # called after every committed registry change, before the event is published

    # ------------------------------------------------------------------------------------------ infra
    def add_change_hook(self, fn) -> None:
        self._hooks.append(fn)

    @property
    def version(self) -> int:
        row = self.db.one("SELECT value FROM meta WHERE key='registry_version'")
        return int(row["value"]) if row else 0

    def audit(self, actor: str, action: str, target: str = "", detail: dict | None = None, result: str = "ok") -> None:
        """Audit-log entry. `detail` must never contain secrets or tokens (callers pass only ids/codes)."""
        text = json.dumps(detail or {}, default=str, sort_keys=True)[:2000]
        with self.db.tx() as c:
            c.execute("INSERT INTO audit_log(ts, actor, action, target, detail, result) VALUES (?,?,?,?,?,?)",
                      (self.clock.now(), sanitize_text(actor, 80) or "unknown", action, target[:80], text, result))

    def list_audit(self, limit: int = 100, action: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        if action:
            rows = self.db.query("SELECT * FROM audit_log WHERE action=? ORDER BY id DESC LIMIT ?", (action, limit))
        else:
            rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"], "target": r["target"],
                 "detail": json.loads(r["detail"]), "result": r["result"]} for r in rows]

    def changed(self, reason: str, endpoint_ids: Iterable[str] = ()) -> None:
        """Bump the registry version, run hooks (scene re-validation), then publish the event. Call after commit."""
        with self.db.tx() as c:
            c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='registry_version'")
        for fn in list(self._hooks):
            try:
                fn(reason)
            except Exception:  # noqa: BLE001
                log.exception("registry change hook failed")
        self.bus.publish("registry_changed", {"version": self.version, "reason": reason,
                                              "endpoint_ids": sorted(set(endpoint_ids))})

    # ------------------------------------------------------------------------------------------ nodes
    def ingest_announce(self, node_id: str, ann: dict) -> AnnounceResult:
        """Store a validated announce. Idempotent: an identical re-announce changes nothing and emits nothing."""
        now = self.clock.now()
        norm = {
            "schema": 2,
            "node_id": node_id,
            "hw": sanitize_text(ann["hw"], 32),
            "fw": sanitize_text(ann["fw"], 32),
            "failsafe_after_s": ann.get("failsafe_after_s", 60),
            "endpoints": [
                {"local_id": ep["local_id"], "type": ep["type"],
                 "capabilities": [normalise_capability(c) for c in ep["capabilities"]]}
                for ep in ann["endpoints"]
            ],
        }
        digest = hashlib.sha256(canonical_json(norm).encode()).hexdigest()
        affected: list[str] = []
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if row is not None and row["deleted"]:
                raise HubError("node_forgotten", "announce from a forgotten node ignored", 409)
            is_new = row is None
            if is_new:
                c.execute("INSERT INTO nodes(node_id, hw, fw, failsafe_after_s, announce_json, announce_hash, first_seen,"
                          " last_seen) VALUES (?,?,?,?,?,?,?,?)",
                          (node_id, norm["hw"], norm["fw"], norm["failsafe_after_s"], json.dumps(norm), digest, now, now))
            elif row["announce_hash"] == digest:
                c.execute("UPDATE nodes SET last_seen=? WHERE node_id=?", (now, node_id))
                return AnnounceResult(False, False, self._pending_ids(c, node_id, norm))
            else:
                c.execute("UPDATE nodes SET hw=?, fw=?, failsafe_after_s=?, announce_json=?, announce_hash=?,"
                          " last_seen=?, provisioned=1 WHERE node_id=?",
                          (norm["hw"], norm["fw"], norm["failsafe_after_s"], json.dumps(norm), digest, now, node_id))
            announced = {ep["local_id"]: ep for ep in norm["endpoints"]}
            for er in c.execute("SELECT * FROM endpoints WHERE node_id=? AND deleted=0", (node_id,)).fetchall():
                ep = announced.get(er["local_id"])
                if ep is None:
                    if er["present"]:
                        c.execute("UPDATE endpoints SET present=0, updated=? WHERE endpoint_id=?", (now, er["endpoint_id"]))
                        affected.append(er["endpoint_id"])
                    continue
                old_caps = {r["cap_id"]: r["cap_json"] for r in
                            c.execute("SELECT cap_id, cap_json FROM capabilities WHERE endpoint_id=?", (er["endpoint_id"],))}
                new_caps = {cap["id"]: json.dumps(cap, sort_keys=True) for cap in ep["capabilities"]}
                if old_caps != new_caps or er["type"] != ep["type"] or not er["present"]:
                    c.execute("DELETE FROM capabilities WHERE endpoint_id=?", (er["endpoint_id"],))
                    for cid, cj in new_caps.items():
                        c.execute("INSERT INTO capabilities(endpoint_id, cap_id, cap_json) VALUES (?,?,?)",
                                  (er["endpoint_id"], cid, cj))
                    c.execute("UPDATE endpoints SET type=?, present=1, updated=? WHERE endpoint_id=?",
                              (ep["type"], now, er["endpoint_id"]))
                    affected.append(er["endpoint_id"])
            pending = self._pending_ids(c, node_id, norm)
        if affected:
            self.changed("announce_changed", affected)
        if pending:
            self.bus.publish("pending_node", {"node_id": node_id, "local_ids": pending})
        return AnnounceResult(is_new, True, pending)

    @staticmethod
    def _pending_ids(c: sqlite3.Connection, node_id: str, norm: dict) -> list[str]:
        bound = {r["local_id"] for r in c.execute("SELECT local_id FROM endpoints WHERE node_id=? AND deleted=0", (node_id,))}
        return [ep["local_id"] for ep in norm["endpoints"] if ep["local_id"] not in bound]

    def node_exists(self, node_id: str, include_deleted: bool = False) -> bool:
        row = self.db.one("SELECT deleted FROM nodes WHERE node_id=?", (node_id,))
        return row is not None and (include_deleted or not row["deleted"])

    def get_node_row(self, node_id: str, include_deleted: bool = False) -> sqlite3.Row:
        row = self.db.one("SELECT * FROM nodes WHERE node_id=?", (node_id,))
        if row is None or (row["deleted"] and not include_deleted):
            raise NotFound("unknown_node", f"no such node {safe_repr(node_id, 40)}")
        return row

    def node_view(self, row: sqlite3.Row) -> dict:
        node_id = row["node_id"]
        eps = [self._view(r) for r in self.db.query(
            "SELECT * FROM endpoints WHERE node_id=? AND deleted=0 ORDER BY created, endpoint_id", (node_id,))]
        announce = json.loads(row["announce_json"]) if row["announce_json"] else {"endpoints": []}
        bound = {e["local_id"] for e in eps}
        pending = [{"local_id": ep["local_id"], "type": ep["type"], "capabilities": ep["capabilities"]}
                   for ep in announce["endpoints"] if ep["local_id"] not in bound]
        if not eps:
            status = "pending"
        else:
            status = "online" if self.live.is_online(node_id) else "offline"
        return {"node_id": node_id, "hw": row["hw"], "fw": row["fw"], "status": status,
                "online": self.link_up() and self.live.is_online(node_id), "provisioned": bool(row["provisioned"]),
                "endpoints": eps, "pending_endpoints": pending, "first_seen": int(row["first_seen"]),
                "last_seen": int(row["last_seen"])}

    def list_nodes(self) -> list[dict]:
        return [self.node_view(r) for r in
                self.db.query("SELECT * FROM nodes WHERE deleted=0 ORDER BY first_seen, node_id")]

    def get_node(self, node_id: str) -> dict:
        return self.node_view(self.get_node_row(node_id))

    def announced_endpoint(self, node_id: str, local_id: str) -> dict | None:
        row = self.get_node_row(node_id)
        if not row["announce_json"]:
            return None
        for ep in json.loads(row["announce_json"])["endpoints"]:
            if ep["local_id"] == local_id:
                return ep
        return None

    # ------------------------------------------------------------------------------------------ endpoints
    def _caps(self, endpoint_id: str) -> list[dict]:
        return [json.loads(r["cap_json"]) for r in self.db.query(
            "SELECT cap_json FROM capabilities WHERE endpoint_id=? ORDER BY rowid", (endpoint_id,))]

    def _view(self, row: sqlite3.Row, with_state: bool = True) -> dict:
        caps = self._caps(row["endpoint_id"])
        node = self.db.one("SELECT provisioned, deleted FROM nodes WHERE node_id=?", (row["node_id"],))
        node_up = bool(node and not node["deleted"] and node["provisioned"] and self.link_up()
                       and self.live.is_online(row["node_id"]))
        view = {
            "endpoint_id": row["endpoint_id"], "node_id": row["node_id"], "local_id": row["local_id"],
            "name": row["name"], "room": row["room"], "aliases": json.loads(row["aliases"]),
            "tags": json.loads(row["tags"]), "type": row["type"], "risk": row["risk"], "online": node_up and bool(row["present"]),
            "enabled": bool(row["enabled"]), "present": bool(row["present"]), "capabilities": caps,
        }
        if with_state:
            view["state"] = self.values_for(row["node_id"], row["local_id"], caps)[0]
        return view

    def get_endpoint(self, endpoint_id: str) -> dict:
        if not isinstance(endpoint_id, str) or not ENDPOINT_ID_RE.match(endpoint_id):
            raise NotFound("unknown_endpoint", "no such endpoint")
        row = self.db.one("SELECT * FROM endpoints WHERE endpoint_id=? AND deleted=0", (endpoint_id,))
        if row is None:
            raise NotFound("unknown_endpoint", "no such endpoint")
        return self._view(row)

    def endpoint_exists(self, endpoint_id: str) -> bool:
        return self.db.one("SELECT 1 FROM endpoints WHERE endpoint_id=? AND deleted=0", (endpoint_id,)) is not None

    def list_endpoints(self, room: str | None = None, etype: str | None = None, include_disabled: bool = True) -> list[dict]:
        sql = "SELECT * FROM endpoints WHERE deleted=0"
        params: list[Any] = []
        if not include_disabled:
            sql += " AND enabled=1"
        if room:
            sql += " AND room = ? COLLATE NOCASE"
            params.append(room)
        if etype:
            sql += " AND type = ?"
            params.append(etype)
        sql += " ORDER BY room COLLATE NOCASE, name COLLATE NOCASE, endpoint_id"
        return [self._view(r) for r in self.db.query(sql, tuple(params))]

    def select(self, room: str | None = None, etype: str | None = None, tag: str | None = None) -> list[dict]:
        """Selector resolution (scenes): enabled endpoints matching ALL given keys. `tag` matches the endpoint's
        admin-set `tags` (exact, tags are lowercase); aliases are not used."""
        out = []
        for ep in self.list_endpoints(room=room, etype=etype, include_disabled=False):
            if tag and tag not in ep["tags"]:
                continue
            out.append(ep)
        return out

    def rooms(self) -> list[str]:
        return [r["name"] for r in self.db.query("SELECT name FROM rooms ORDER BY name COLLATE NOCASE")]

    @staticmethod
    def _canon_room(c: sqlite3.Connection, room: str) -> str:
        row = c.execute("SELECT name FROM rooms WHERE name = ? COLLATE NOCASE", (room,)).fetchone()
        if row:
            return row["name"]
        c.execute("INSERT INTO rooms(name) VALUES (?)", (room,))
        return room

    @staticmethod
    def _check_name_free(c: sqlite3.Connection, room: str, name: str, exclude: str | None = None) -> None:
        row = c.execute("SELECT endpoint_id FROM endpoints WHERE deleted=0 AND room = ? COLLATE NOCASE"
                        " AND name = ? COLLATE NOCASE AND endpoint_id != ?", (room, name, exclude or "")).fetchone()
        if row:
            raise Conflict("name_conflict", f"an endpoint named '{name}' already exists in room '{room}'")

    def approve_node(self, node_id: str, items: list[dict], actor: str) -> list[dict]:
        """Name and place announced endpoints; assigns permanent endpoint_ids. All-or-nothing."""
        if not isinstance(items, list) or not items:
            raise HubError("bad_request", "endpoints must be a non-empty list")
        now = self.clock.now()
        created: list[str] = []
        with self.db.tx() as c:
            row = self.get_node_row(node_id)
            if not row["announce_json"]:
                raise Conflict("no_announce", "node has not announced yet")
            announced = {ep["local_id"]: ep for ep in json.loads(row["announce_json"])["endpoints"]}
            bound = {r["local_id"] for r in c.execute("SELECT local_id FROM endpoints WHERE node_id=? AND deleted=0", (node_id,))}
            seen: set[str] = set()
            for it in items:
                if not isinstance(it, dict):
                    raise HubError("bad_request", "each endpoint entry must be an object")
                unknown = set(it) - {"local_id", "name", "room", "aliases", "tags", "risk"}
                if unknown:
                    raise HubError("bad_request", f"unknown fields: {sorted(unknown)}")
                lid = it.get("local_id")
                if lid not in announced:
                    raise HubError("unknown_local_id", f"node did not announce local_id {safe_repr(lid, 20)}")
                if lid in bound or lid in seen:
                    raise Conflict("already_approved", f"local_id {lid} is already approved")
                seen.add(lid)
                name = clean_label(it.get("name"), "name")
                room = self._canon_room(c, clean_label(it.get("room"), "room"))
                risk = it.get("risk", "normal")
                if risk not in ("normal", "confirm"):
                    raise HubError("bad_request", "risk must be normal or confirm")
                self._check_name_free(c, room, name)
                eid = new_endpoint_id()
                while c.execute("SELECT 1 FROM endpoints WHERE endpoint_id=?", (eid,)).fetchone():
                    eid = new_endpoint_id()
                ep = announced[lid]
                c.execute("INSERT INTO endpoints(endpoint_id, node_id, local_id, type, name, room, aliases, tags, risk,"
                          " enabled, present, created, updated) VALUES (?,?,?,?,?,?,?,?,?,1,1,?,?)",
                          (eid, node_id, lid, ep["type"], name, room, json.dumps(clean_aliases(it.get("aliases"))),
                           json.dumps(clean_tags(it.get("tags"))), risk, now, now))
                for cap in ep["capabilities"]:
                    c.execute("INSERT INTO capabilities(endpoint_id, cap_id, cap_json) VALUES (?,?,?)",
                              (eid, cap["id"], json.dumps(cap, sort_keys=True)))
                created.append(eid)
        self.audit(actor, "node.approve", node_id, {"endpoint_ids": created})
        self.changed("approve", created)
        return [self.get_endpoint(e) for e in created]

    def patch_endpoint(self, endpoint_id: str, patch: dict, actor: str) -> dict:
        if not isinstance(patch, dict) or not patch:
            raise HubError("bad_request", "empty patch")
        unknown = set(patch) - {"name", "room", "aliases", "tags", "risk", "enabled"}
        if unknown:
            raise HubError("bad_request", f"unknown fields: {sorted(unknown)}")
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM endpoints WHERE endpoint_id=? AND deleted=0", (endpoint_id,)).fetchone()
            if row is None:
                raise NotFound("unknown_endpoint", "no such endpoint")
            name = clean_label(patch["name"], "name") if "name" in patch else row["name"]
            room = self._canon_room(c, clean_label(patch["room"], "room")) if "room" in patch else row["room"]
            aliases = json.dumps(clean_aliases(patch["aliases"])) if "aliases" in patch else row["aliases"]
            tags = json.dumps(clean_tags(patch["tags"])) if "tags" in patch else row["tags"]
            risk = patch.get("risk", row["risk"])
            if risk not in ("normal", "confirm"):
                raise HubError("bad_request", "risk must be normal or confirm")
            enabled = patch.get("enabled", bool(row["enabled"]))
            if not isinstance(enabled, bool):
                raise HubError("bad_request", "enabled must be a boolean")
            if name != row["name"] or room != row["room"]:
                self._check_name_free(c, room, name, exclude=endpoint_id)
            c.execute("UPDATE endpoints SET name=?, room=?, aliases=?, tags=?, risk=?, enabled=?, updated=? WHERE endpoint_id=?",
                      (name, room, aliases, tags, risk, int(enabled), self.clock.now(), endpoint_id))
        detail = {k: patch[k] for k in patch if k in ("risk", "enabled", "room")}
        self.audit(actor, "endpoint.patch", endpoint_id, {"fields": sorted(patch), **detail})
        self.changed("patch", [endpoint_id])
        return self.get_endpoint(endpoint_id)

    def rebind_endpoint(self, endpoint_id: str, node_id: str, local_id: str, actor: str) -> dict:
        """Hardware swap: the endpoint_id stays, it now points at another announced (node, local_id) of the same type."""
        if not NODE_ID_RE.match(str(node_id)):
            raise HubError("bad_request", "invalid node_id")
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM endpoints WHERE endpoint_id=? AND deleted=0", (endpoint_id,)).fetchone()
            if row is None:
                raise NotFound("unknown_endpoint", "no such endpoint")
            ep = self.announced_endpoint(node_id, local_id)
            if ep is None:
                raise HubError("unknown_local_id", "target node has not announced that local_id", 404)
            if ep["type"] != row["type"]:
                raise Conflict("type_mismatch", f"endpoint is a {row['type']}, target is a {ep['type']}")
            other = c.execute("SELECT endpoint_id FROM endpoints WHERE node_id=? AND local_id=? AND deleted=0",
                              (node_id, local_id)).fetchone()
            if other and other["endpoint_id"] != endpoint_id:
                raise Conflict("already_bound", f"target is bound to {other['endpoint_id']}")
            c.execute("UPDATE endpoints SET node_id=?, local_id=?, type=?, present=1, updated=? WHERE endpoint_id=?",
                      (node_id, local_id, ep["type"], self.clock.now(), endpoint_id))
            c.execute("DELETE FROM capabilities WHERE endpoint_id=?", (endpoint_id,))
            for cap in ep["capabilities"]:
                c.execute("INSERT INTO capabilities(endpoint_id, cap_id, cap_json) VALUES (?,?,?)",
                          (endpoint_id, cap["id"], json.dumps(cap, sort_keys=True)))
        self.audit(actor, "endpoint.rebind", endpoint_id, {"node_id": node_id, "local_id": local_id,
                                                          "from_node": row["node_id"], "from_local": row["local_id"]})
        self.changed("rebind", [endpoint_id])
        return self.get_endpoint(endpoint_id)

    def set_provisioned(self, node_id: str, provisioned: bool) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE nodes SET provisioned=? WHERE node_id=?", (int(provisioned), node_id))
        if not provisioned:
            self.live.set_online(node_id, False)

    def soft_delete_node(self, node_id: str, revocation_pending: bool) -> list[str]:
        """Forget: soft-delete node and its endpoints (ids stay reserved), drop live data."""
        now = self.clock.now()
        with self.db.tx() as c:
            eids = [r["endpoint_id"] for r in c.execute(
                "SELECT endpoint_id FROM endpoints WHERE node_id=? AND deleted=0", (node_id,))]
            c.execute("UPDATE endpoints SET deleted=1, updated=? WHERE node_id=? AND deleted=0", (now, node_id))
            c.execute("UPDATE nodes SET deleted=1, provisioned=0, revocation_pending=? WHERE node_id=?",
                      (int(revocation_pending), node_id))
            c.execute("UPDATE provision_sessions SET state='cancelled' WHERE node_id=? AND state IN ('pending','connected')",
                      (node_id,))
        self.live.drop_node(node_id)
        return eids

    def nodes_pending_revocation(self) -> list[str]:
        return [r["node_id"] for r in self.db.query("SELECT node_id FROM nodes WHERE deleted=1 AND revocation_pending=1")]

    def clear_revocation_pending(self, node_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE nodes SET revocation_pending=0 WHERE node_id=?", (node_id,))

    # ------------------------------------------------------------------------------------------ live values
    def values_for(self, node_id: str, local_id: str, caps: list[dict]) -> tuple[dict, int]:
        """Last known values for an endpoint, filtered to declared capabilities and type-checked against them."""
        got = self.live.get_values(node_id, local_id)
        if not got:
            return {}, 0
        raw, ts = got
        out: dict[str, Any] = {}
        by_id = {c["id"]: c for c in caps}
        for cid, v in raw.items():
            cap = by_id.get(cid)
            if cap is None:
                continue
            ok, _code, val = coerce_value(cap, v)
            if ok:
                out[cid] = val
        return out, ts

    def ingest_state(self, node_id: str, doc: dict) -> list[str]:
        """Apply a validated state payload. Returns the local_ids whose values changed (state dedup)."""
        changed: list[str] = []
        ts = int(doc["ts"])
        for local_id, values in doc["endpoints"].items():
            if self.live.update_values(node_id, local_id, values, ts):
                changed.append(local_id)
        return changed

    def endpoint_types(self) -> tuple[str, ...]:
        return ENDPOINT_TYPES
