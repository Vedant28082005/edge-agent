"""Provisioning sessions (BLE flow step 4-7): create the node's MQTT user via dynsec, return the bundle, match the
node's announce to the session, clean up sessions whose board never connected (TTL, default 10 min).

Secrets (MQTT password, Wi-Fi password) exist only in the returned bundle and in memory: they are registered with the
log redactor, never written to the DB or the audit log.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import HubConfig
from .dynsec import DynsecClient, DynsecError
from .errors import Conflict, HubError, NotFound
from .registry import Registry
from .util import NODE_ID_RE, SECRETS, Clock, new_session_id, random_secret, safe_repr

log = logging.getLogger("device_hub.provisioning")

BUNDLE_MAX_BYTES = 4096    # ble-provisioning.md


class Provisioning:
    def __init__(self, registry: Registry, dynsec: DynsecClient, cfg: HubConfig, clock: Clock) -> None:
        self.registry = registry
        self.db = registry.db
        self.dynsec = dynsec
        self.cfg = cfg
        self.clock = clock

    # ------------------------------------------------------------------------------------------ create
    def _ca_pem(self) -> str:
        path = self.cfg.provision.ca_file or self.cfg.mqtt.ca_file
        if not path:
            raise HubError("not_configured", "provision.ca_file / mqtt.ca_file is not configured", 500)
        pem = open(path, encoding="ascii").read().strip() + "\n"
        if "PRIVATE KEY" in pem:
            raise HubError("not_configured", "refusing to put a private key in the bundle", 500)
        return pem

    def create_session(self, node_id: Any, wifi: Any, actor: str) -> dict:
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            raise HubError("bad_request", "node_id must match esp32-<12 hex>")
        if not isinstance(wifi, dict) or set(wifi) != {"ssid", "password"} \
                or not isinstance(wifi["ssid"], str) or not 1 <= len(wifi["ssid"]) <= 32 \
                or not isinstance(wifi["password"], str) or len(wifi["password"]) > 64:
            raise HubError("bad_request", "wifi must be {ssid (1-32 chars), password (max 64 chars)}")
        pv = self.cfg.provision
        if not pv.broker_host:
            raise HubError("not_configured", "provision.broker_host is not configured", 500)
        SECRETS.add(wifi["password"], permanent=False)
        row = self.db.one("SELECT * FROM nodes WHERE node_id=?", (node_id,))
        if row is not None and not row["deleted"] and row["provisioned"] and self.registry.live.is_online(node_id):
            raise Conflict("node_online", "node is connected; unprovision it first")
        ca_pem = self._ca_pem()
        password = random_secret(24)
        SECRETS.add(password, permanent=False)
        bundle: dict[str, Any] = {
            "v": 1,
            "wifi": {"ssid": wifi["ssid"], "password": wifi["password"]},
            "mqtt": {"host": pv.broker_host, "port": pv.broker_port, "username": node_id, "password": password},
            "ntp_host": pv.ntp_host or pv.broker_host,
            "ca_pem": ca_pem,
        }
        if len(json.dumps(bundle, separators=(",", ":")).encode()) > BUNDLE_MAX_BYTES:
            raise HubError("bundle_too_large", f"bundle exceeds {BUNDLE_MAX_BYTES} bytes", 500)

        self.dynsec.create_node_user(node_id, password)       # raises DynsecError (502/503/504) on failure
        sid = new_session_id()
        now = self.clock.now()
        try:
            with self.db.tx() as c:
                c.execute("UPDATE provision_sessions SET state='cancelled' WHERE node_id=? AND state IN ('pending','connected')",
                          (node_id,))
                if row is not None and (row["deleted"] or not row["provisioned"]):
                    # re-provisioning a forgotten / unprovisioned node: start from a clean slate
                    c.execute("UPDATE nodes SET deleted=0, provisioned=1, announce_json=NULL, announce_hash=NULL,"
                              " revocation_pending=0 WHERE node_id=?", (node_id,))
                c.execute("INSERT INTO provision_sessions(session_id, node_id, mqtt_username, state, created_by, created,"
                          " expires_at) VALUES (?,?,?,?,?,?,?)",
                          (sid, node_id, node_id, "pending", actor[:64], now, now + pv.ttl_s))
        except Exception:
            try:
                self.dynsec.delete_node_user(node_id)
            except DynsecError:
                log.exception("could not roll back MQTT user for %s", node_id)
            raise
        self.registry.audit(actor, "provision.create", node_id, {"session_id": sid})
        log.info("provision session %s created for %s (ttl %ds)", sid, node_id, pv.ttl_s)
        return {"session_id": sid, "expires_at": int(now + pv.ttl_s), "bundle": bundle}

    # ------------------------------------------------------------------------------------------ match / cleanup
    def on_announce(self, node_id: str) -> None:
        """A valid announce arrived: if a pending session exists for this node, mark it connected."""
        with self.db.tx() as c:
            cur = c.execute("UPDATE provision_sessions SET state='connected', connected_at=? WHERE node_id=? "
                            "AND state='pending' AND expires_at > ?", (self.clock.now(), node_id, self.clock.now()))
            matched = cur.rowcount
        if matched:
            self.registry.audit("system", "provision.connected", node_id)
            log.info("provision session matched announce from %s", node_id)

    def cleanup_expired(self) -> list[str]:
        """Delete sessions that were never connected within the TTL, with their MQTT user and role."""
        now = self.clock.now()
        done: list[str] = []
        for r in self.db.query("SELECT session_id, node_id FROM provision_sessions WHERE state='pending' AND expires_at <= ?",
                               (now,)):
            try:
                self.dynsec.delete_node_user(r["node_id"])
            except HubError as e:
                log.warning("cleanup of %s postponed: %s", r["node_id"], e.message)
                continue
            with self.db.tx() as c:
                c.execute("UPDATE provision_sessions SET state='expired' WHERE session_id=?", (r["session_id"],))
            self.registry.audit("system", "provision.expire", r["node_id"], {"session_id": r["session_id"]})
            log.info("provision session %s for %s expired; MQTT user removed", r["session_id"], r["node_id"])
            done.append(r["session_id"])
        return done

    def cancel(self, node_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE provision_sessions SET state='cancelled' WHERE node_id=? AND state IN ('pending','connected')",
                      (node_id,))

    # ------------------------------------------------------------------------------------------ queries
    @staticmethod
    def _view(r) -> dict:
        return {"session_id": r["session_id"], "node_id": r["node_id"], "state": r["state"],
                "created": int(r["created"]), "expires_at": int(r["expires_at"])}

    def list_sessions(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM provision_sessions" + (" WHERE state IN ('pending','connected')" if active_only else "") \
            + " ORDER BY created DESC LIMIT 100"
        return [self._view(r) for r in self.db.query(sql)]

    def get_session(self, session_id: str) -> dict:
        r = self.db.one("SELECT * FROM provision_sessions WHERE session_id=?", (session_id,))
        if r is None:
            raise NotFound("unknown_session", f"no such session {safe_repr(session_id, 24)}")
        return self._view(r)
