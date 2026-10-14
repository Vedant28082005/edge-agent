"""Scenes (plan 4.7): fixed-id and selector targets, per-action results, skip-and-report, never queue.

Run semantics (mcp-tools.md run_scene):
  * resolve every action to concrete (endpoint, capability, value) and pre-flight each one;
  * if ANY resolved action targets a risk:confirm endpoint and no valid token covers exactly those actions, nothing
    runs at all (not even the non-risky actions) -> status needs_confirmation;
  * otherwise run through the same executor as set_state; offline / incapable endpoints are skipped and reported.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .contracts import ENDPOINT_TYPES, VOCAB, coerce_value
from .errors import Conflict, HubError, NotFound
from .events import EventBus
from .executor import Executor, make_by
from .registry import TAG_RE, Registry, clean_label
from .util import (ENDPOINT_ID_RE, Clock, action_hash, new_scene_id, run_scene_action, safe_repr, sanitize_text)

log = logging.getLogger("device_hub.scenes")

MAX_ACTIONS = 50
SELECTOR_KEYS = {"room", "type", "tag"}


@dataclass
class Resolved:
    index: int
    ep: dict
    capability: str
    value: Any
    status: str = "pending"        # pending | done | moving | skipped_offline | skipped_no_capability | failed
    detail: str | None = None
    cap: dict | None = None
    coerced: Any = None


def _is_primitive(v: Any) -> bool:
    return (isinstance(v, bool) or (isinstance(v, (int, float)) and math.isfinite(v))
            or (isinstance(v, str) and len(v) <= 24))


class SceneService:
    def __init__(self, registry: Registry, executor: Executor, bus: EventBus, clock: Clock, workers: int = 8) -> None:
        self.registry = registry
        self.db = registry.db
        self.executor = executor
        self.bus = bus
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scene")
        registry.add_change_hook(self.revalidate_all)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------------------------------ validation
    @staticmethod
    def normalise_actions(actions: Any) -> list[dict]:
        if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
            raise HubError("bad_request", f"actions must be a list of 1..{MAX_ACTIONS} items")
        out = []
        for a in actions:
            if not isinstance(a, dict) or set(a) != {"target", "capability", "value"}:
                raise HubError("bad_request", "each action needs exactly target, capability, value")
            t = a["target"]
            if not isinstance(t, dict) or not t:
                raise HubError("bad_request", "target must be {endpoint_id} or a selector {room?, type?, tag?}")
            if "endpoint_id" in t:
                if set(t) != {"endpoint_id"} or not isinstance(t["endpoint_id"], str) or not ENDPOINT_ID_RE.match(t["endpoint_id"]):
                    raise HubError("bad_request", "fixed target must be exactly {endpoint_id: ep_xxxxxxxx}")
                target = {"endpoint_id": t["endpoint_id"]}
            else:
                if not set(t) <= SELECTOR_KEYS:
                    raise HubError("bad_request", f"selector keys must be within {sorted(SELECTOR_KEYS)}")
                target = {}
                for k, v in t.items():
                    if not isinstance(v, str):
                        raise HubError("bad_request", f"selector {k} must be a string")
                    if k == "tag":
                        if not TAG_RE.match(v):
                            raise HubError("bad_request", "selector tag must be 1..24 characters of [a-z0-9_-]")
                        target[k] = v
                        continue
                    v = sanitize_text(v, 48)
                    if not v:
                        raise HubError("bad_request", f"selector {k} is empty")
                    if k == "type" and v not in ENDPOINT_TYPES:
                        raise HubError("bad_request", f"unknown endpoint type {safe_repr(v, 20)}")
                    target[k] = v
            cap_id, value = a["capability"], a["value"]
            spec = VOCAB.get(cap_id) if isinstance(cap_id, str) else None
            if spec is None or spec["kind"] != "actuator":
                raise HubError("bad_request", f"capability {safe_repr(cap_id, 24)} is not an actuator capability")
            if not _is_primitive(value):
                raise HubError("bad_request", "value must be a bool, finite number or short string")
            if cap_id != "mode":     # mode values are per-device; checked at run time
                ok, code, _ = coerce_value({**spec, "id": cap_id}, value)
                if not ok:
                    raise HubError("bad_request", f"value {safe_repr(value, 24)} invalid for {cap_id}: {code}")
            out.append({"target": target, "capability": cap_id, "value": value})
        return out

    def _validate_fixed_targets(self, actions: list[dict]) -> None:
        for a in actions:
            eid = a["target"].get("endpoint_id")
            if eid and not self.registry.endpoint_exists(eid):
                raise HubError("unknown_endpoint", f"target endpoint {eid} does not exist", 404)

    # ------------------------------------------------------------------------------------------ CRUD
    def _row_to_scene(self, row: sqlite3.Row) -> dict:
        actions = [{"target": json.loads(r["target_json"]), "capability": r["capability"], "value": json.loads(r["value_json"])}
                   for r in self.db.query("SELECT * FROM scene_actions WHERE scene_id=? ORDER BY idx", (row["scene_id"],))]
        return {"scene_id": row["scene_id"], "name": row["name"], "owner": row["owner"], "broken": bool(row["broken"]),
                "broken_reasons": json.loads(row["broken_reasons"]), "actions": actions, "rooms": self._rooms_of(actions)}

    def _rooms_of(self, actions: list[dict]) -> list[str]:
        """Rooms the scene touches, so the gateway can check a member's room access. '*' = a selector without room."""
        rooms: set[str] = set()
        for a in actions:
            t = a["target"]
            if "endpoint_id" in t:
                try:
                    rooms.add(self.registry.get_endpoint(t["endpoint_id"])["room"])
                except NotFound:
                    pass
            else:
                rooms.add(t.get("room", "*"))
        return sorted(rooms, key=str.lower)

    def get_scene(self, ref: Any) -> dict:
        if not isinstance(ref, str) or not ref:
            raise NotFound("unknown_scene", "no such scene")
        row = self.db.one("SELECT * FROM scenes WHERE deleted=0 AND scene_id=?", (ref,)) \
            or self.db.one("SELECT * FROM scenes WHERE deleted=0 AND name=?", (ref,))
        if row is None:
            raise NotFound("unknown_scene", "no such scene")
        return self._row_to_scene(row)

    def list_scenes(self) -> list[dict]:
        return [self._row_to_scene(r) for r in self.db.query("SELECT * FROM scenes WHERE deleted=0 ORDER BY name COLLATE NOCASE")]

    def _store_actions(self, c: sqlite3.Connection, scene_id: str, actions: list[dict]) -> None:
        c.execute("DELETE FROM scene_actions WHERE scene_id=?", (scene_id,))
        for i, a in enumerate(actions):
            c.execute("INSERT INTO scene_actions(scene_id, idx, target_json, capability, value_json) VALUES (?,?,?,?,?)",
                      (scene_id, i, json.dumps(a["target"], sort_keys=True), a["capability"], json.dumps(a["value"])))

    def create_scene(self, name: Any, actions: Any, owner: str, actor: str) -> dict:
        name = clean_label(name, "name")
        acts = self.normalise_actions(actions)
        self._validate_fixed_targets(acts)
        sid = new_scene_id()
        now = self.clock.now()
        try:
            with self.db.tx() as c:
                c.execute("INSERT INTO scenes(scene_id, name, owner, created, updated) VALUES (?,?,?,?,?)",
                          (sid, name, sanitize_text(owner, 64), now, now))
                self._store_actions(c, sid, acts)
        except sqlite3.IntegrityError:
            raise Conflict("name_conflict", f"a scene named '{name}' already exists") from None
        self.registry.audit(actor, "scene.create", sid, {"actions": len(acts)})
        self.revalidate_all("scene_create", only=sid)
        self.bus.publish("scenes_changed", {"reason": "create", "scene_ids": [sid]})
        return self.get_scene(sid)

    def update_scene(self, scene_id: str, name: Any, actions: Any, actor: str) -> dict:
        cur = self.get_scene(scene_id)
        new_name = clean_label(name, "name") if name is not None else cur["name"]
        acts = self.normalise_actions(actions) if actions is not None else cur["actions"]
        if actions is not None:
            self._validate_fixed_targets(acts)
        try:
            with self.db.tx() as c:
                c.execute("UPDATE scenes SET name=?, updated=? WHERE scene_id=?", (new_name, self.clock.now(), scene_id))
                self._store_actions(c, scene_id, acts)
        except sqlite3.IntegrityError:
            raise Conflict("name_conflict", f"a scene named '{new_name}' already exists") from None
        self.registry.audit(actor, "scene.update", scene_id, {"actions": len(acts)})
        self.revalidate_all("scene_update", only=scene_id)
        self.bus.publish("scenes_changed", {"reason": "update", "scene_ids": [scene_id]})
        return self.get_scene(scene_id)

    def delete_scene(self, scene_id: str, actor: str) -> None:
        self.get_scene(scene_id)
        with self.db.tx() as c:
            c.execute("UPDATE scenes SET deleted=1, updated=? WHERE scene_id=?", (self.clock.now(), scene_id))
        self.registry.audit(actor, "scene.delete", scene_id)
        self.bus.publish("scenes_changed", {"reason": "delete", "scene_ids": [scene_id]})

    # ------------------------------------------------------------------------------------------ resolution
    def resolve(self, scene: dict) -> list[Resolved]:
        out: list[Resolved] = []
        for a in scene["actions"]:
            t = a["target"]
            if "endpoint_id" in t:
                try:
                    eps = [self.registry.get_endpoint(t["endpoint_id"])]
                except NotFound:
                    eps = []
            else:
                eps = self.registry.select(t.get("room"), t.get("type"), t.get("tag"))
            for ep in eps:
                out.append(Resolved(len(out), ep, a["capability"], a["value"]))
        return out

    def risky_actions(self, scene: dict, resolved: list[Resolved] | None = None) -> list[dict]:
        resolved = self.resolve(scene) if resolved is None else resolved
        return [{"endpoint_id": r.ep["endpoint_id"], "capability": r.capability, "value": r.value}
                for r in resolved if r.ep["risk"] == "confirm"]

    def action_for_scene(self, scene_ref: str) -> dict:
        """The canonical run_scene action the gateway needs to mint a token (also used by run)."""
        scene = self.get_scene(scene_ref)
        action = run_scene_action(scene["scene_id"], self.risky_actions(scene))
        return {"action": action, "action_hash": action_hash(action), "needs_confirmation": action["actions"]}

    # ------------------------------------------------------------------------------------------ validation of stored scenes
    def _broken_reasons(self, scene: dict) -> list[str]:
        reasons: list[str] = []
        for a in scene["actions"]:
            t = a["target"]
            if "endpoint_id" not in t:
                continue
            try:
                ep = self.registry.get_endpoint(t["endpoint_id"])
            except NotFound:
                reasons.append(f"endpoint {t['endpoint_id']} no longer exists")
                continue
            cap = next((c for c in ep["capabilities"] if c["id"] == a["capability"]), None)
            if cap is None or cap["kind"] != "actuator":
                reasons.append(f"{t['endpoint_id']} has no actuator capability {a['capability']}")
                continue
            ok, code, _ = coerce_value(cap, a["value"])
            if not ok:
                reasons.append(f"{t['endpoint_id']}.{a['capability']}: value no longer valid ({code})")
        return sorted(set(reasons))

    def revalidate_all(self, reason: str = "registry_change", only: str | None = None) -> list[str]:
        """Re-check every scene against the live registry and flag/unflag broken ones. Returns changed scene ids."""
        changed: list[str] = []
        rows = self.db.query("SELECT * FROM scenes WHERE deleted=0" + (" AND scene_id=?" if only else ""),
                             (only,) if only else ())
        for row in rows:
            scene = self._row_to_scene(row)
            reasons = self._broken_reasons(scene)
            broken = bool(reasons)
            if broken != bool(row["broken"]) or json.dumps(reasons) != row["broken_reasons"]:
                with self.db.tx() as c:
                    c.execute("UPDATE scenes SET broken=?, broken_reasons=? WHERE scene_id=?",
                              (int(broken), json.dumps(reasons), row["scene_id"]))
                changed.append(row["scene_id"])
                log.info("scene %s %s (%s)", row["scene_id"], "flagged broken" if broken else "repaired", reason)
        if changed and not only:
            self.bus.publish("scenes_changed", {"reason": reason, "scene_ids": changed})
        return changed

    # ------------------------------------------------------------------------------------------ usage (forget)
    def scenes_using_endpoints(self, endpoint_ids: set[str]) -> list[dict]:
        used = []
        for scene in self.list_scenes():
            hit = False
            for a in scene["actions"]:
                t = a["target"]
                if "endpoint_id" in t:
                    hit = t["endpoint_id"] in endpoint_ids
                else:
                    hit = any(ep["endpoint_id"] in endpoint_ids
                              for ep in self.registry.select(t.get("room"), t.get("type"), t.get("tag")))
                if hit:
                    break
            if hit:
                used.append({"scene_id": scene["scene_id"], "name": scene["name"]})
        return used

    # ------------------------------------------------------------------------------------------ run
    def run_scene(self, ref: Any, user_id: str | None = None, token: str | None = None, call_id: str | None = None) -> dict:
        try:
            scene = self.get_scene(ref)
        except NotFound:
            return {"ok": False, "executed": False, "error": "unknown_scene"}
        res = self._run(scene, user_id, token, call_id)
        self.registry.audit(make_by(user_id), "scene.run", scene["scene_id"], {
            "executed": res.get("executed"), "status": res.get("status") or res.get("error"),
            "results": {s: sum(1 for r in res.get("results", []) if r["status"] == s)
                        for s in ("done", "moving", "skipped_offline", "skipped_no_capability", "failed")} if res.get("executed") else {}},
            "ok" if res.get("executed") else "denied")
        return res

    def _run(self, scene: dict, user_id: str | None, token: str | None, call_id: str | None = None) -> dict:
        if scene["broken"]:
            return {"ok": False, "executed": False, "error": "scene_broken", "detail": scene["broken_reasons"]}
        items = self.resolve(scene)
        # 1) all-or-nothing confirmation gate
        risky = self.risky_actions(scene, items)
        if risky:
            action = run_scene_action(scene["scene_id"], risky)
            if not self.executor.confirm_ok(action, user_id, token):
                return {"ok": False, "executed": False, "status": "needs_confirmation", "scene_id": scene["scene_id"],
                        "needs_confirmation": action["actions"], "action_hash": action_hash(action)}
        # 2) pre-flight each action
        for it in items:
            cap, coerced, err = self.executor.check_action(it.ep, it.capability, it.value)
            if err == "disabled":
                it.status, it.detail = "failed", "disabled"
            elif err in ("unknown_capability", "not_actuator"):
                it.status = "skipped_no_capability"
            elif err:
                it.status, it.detail = "failed", err
            elif not it.ep["online"]:
                it.status = "skipped_offline"
            else:
                it.cap, it.coerced = cap, coerced
        # 3) execute: sequential per node (one board at a time), different nodes in parallel
        by = make_by(None, scene["scene_id"])
        by_node: dict[str, list[Resolved]] = {}
        for it in items:
            if it.status == "pending":
                by_node.setdefault(it.ep["node_id"], []).append(it)

        def run_node(batch: list[Resolved]) -> None:
            for it in batch:
                r = self.executor.apply(it.ep, it.cap, it.coerced, by, it.value, call_id)  # type: ignore[arg-type]
                it.status = r["status"] if r["ok"] else "failed"          # done | moving | failed
                it.detail = None if r["ok"] else r.get("detail") or r["error"]

        futures = [self._pool.submit(run_node, batch) for batch in by_node.values()]
        for f in futures:
            f.result()
        results = []
        for it in items:
            entry = {"endpoint_id": it.ep["endpoint_id"], "capability": it.capability, "status": it.status}
            if it.detail:
                entry["detail"] = it.detail
            results.append(entry)
        return {"ok": all(r["status"] != "failed" for r in results), "executed": True,
                "scene": scene["scene_id"], "scene_name": scene["name"], "results": results}
