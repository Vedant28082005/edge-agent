"""Admin operations (plan 4.6). Reachable only through the internal HTTP API (which the gateway guards with the admin
role). None of this is exposed to the LLM: the MCP server has no reference to this module.

Forget (real revocation) order: usage check -> delete MQTT user+role via dynsec (kicks the client) -> clear retained
topics -> soft-delete registry rows -> re-validate scenes (dependents become broken, never deleted) -> audit.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .bridge import MqttBridge
from .dynsec import DynsecClient
from .errors import Conflict, HubError, NotFound
from .executor import Executor
from .provisioning import Provisioning
from .registry import Registry
from .scenes import SceneService
from .util import NODE_ID_RE, safe_repr

log = logging.getLogger("device_hub.admin")


class AdminService:
    def __init__(self, registry: Registry, bridge: MqttBridge, dynsec: DynsecClient, provisioning: Provisioning,
                 executor: Executor, scenes: SceneService) -> None:
        self.registry = registry
        self.bridge = bridge
        self.dynsec = dynsec
        self.provisioning = provisioning
        self.executor = executor
        self.scenes = scenes

    @staticmethod
    def _node_id(node_id: Any) -> str:
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            raise HubError("bad_request", "invalid node_id")
        return node_id

    # ---- provisioning / listing
    def create_provision_session(self, body: dict, actor: str) -> dict:
        if not isinstance(body, dict) or "node_id" not in body:
            raise HubError("bad_request", "node_id required")
        if "wifi" not in body:
            raise HubError("bad_request", "wifi {ssid, password} required (the gateway resolves wifi_profile before calling)")
        return self.provisioning.create_session(body["node_id"], body["wifi"], actor)

    def list_nodes(self) -> list[dict]:
        return self.registry.list_nodes()

    def get_node(self, node_id: str) -> dict:
        return self.registry.get_node(self._node_id(node_id))

    # ---- endpoints
    def approve_node(self, node_id: str, body: dict, actor: str) -> list[dict]:
        if not isinstance(body, dict) or set(body) != {"endpoints"}:
            raise HubError("bad_request", "body must be {endpoints: [...]}")
        return self.registry.approve_node(self._node_id(node_id), body["endpoints"], actor)

    def patch_endpoint(self, endpoint_id: str, body: dict, actor: str) -> dict:
        return self.registry.patch_endpoint(endpoint_id, body, actor)

    def rebind_endpoint(self, endpoint_id: str, body: dict, actor: str) -> dict:
        if not isinstance(body, dict) or set(body) != {"node_id", "local_id"}:
            raise HubError("bad_request", "body must be {node_id, local_id}")
        return self.registry.rebind_endpoint(endpoint_id, body["node_id"], body["local_id"], actor)

    # ---- node commands
    def identify_node(self, node_id: str, duration_s: Any, actor: str) -> dict:
        node_id = self._node_id(node_id)
        d = 10 if duration_s is None else duration_s
        if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 60:
            raise HubError("bad_request", "duration_s must be an integer 1..60")
        self.registry.get_node_row(node_id)
        if not self.registry.live.is_online(node_id):
            raise Conflict("offline", "node is offline")
        res = self.executor.node_command(node_id, {"cmd": "identify", "duration_s": d})
        self.registry.audit(actor, "node.identify", node_id, {"duration_s": d, "acked": res["acked"]})
        return res

    def unprovision_node(self, node_id: str, force: bool, actor: str) -> dict:
        """Wipe the board's config (cmd unprovision) and revoke its MQTT credentials. Registry rows are kept so the
        endpoint ids survive a re-provision. An offline board cannot be told: only with force=true are its
        credentials revoked anyway."""
        node_id = self._node_id(node_id)
        self.registry.get_node_row(node_id)
        res: dict[str, Any] = {"sent": False, "acked": False}
        if self.registry.live.is_online(node_id):
            res = self.executor.node_command(node_id, {"cmd": "unprovision"})
        elif not force:
            raise Conflict("offline", "node is offline and cannot be told to wipe itself; use force to revoke its credentials only")
        self.dynsec.delete_node_user(node_id)
        self.registry.set_provisioned(node_id, False)
        self.registry.bus.publish("node_status", {"node_id": node_id, "online": False})
        self.registry.audit(actor, "node.unprovision", node_id, {"sent": res["sent"], "acked": res["acked"], "force": force})
        self.registry.changed("unprovision", [e["endpoint_id"] for e in self.registry.get_node(node_id)["endpoints"]])
        return res

    # ---- forget
    def forget_usage(self, node_id: str) -> dict:
        node_id = self._node_id(node_id)
        node = self.registry.get_node(node_id)
        eids = {e["endpoint_id"] for e in node["endpoints"]}
        scenes = self.scenes.scenes_using_endpoints(eids)
        automations = 0     # no automations table yet (agentic phase)
        n = len(scenes)
        return {"scenes": n, "automations": automations, "scene_list": scenes,
                "message": f"used by {n} scene{'s' if n != 1 else ''}, {automations} automation{'s' if automations != 1 else ''}"}

    def forget_node(self, node_id: str, confirm: bool, force: bool, actor: str) -> dict:
        """Without confirm: returns {"forgotten": False, usage...} and changes nothing. With confirm: revokes
        credentials and removes the node. `force`: still soft-delete the registry rows when the broker/dynsec is
        unreachable; the revocation is then retried by the maintenance loop until it succeeds."""
        node_id = self._node_id(node_id)
        if not self.registry.node_exists(node_id):
            return self._cancel_unannounced(node_id, confirm, actor)
        usage = self.forget_usage(node_id)
        if not confirm:
            return {"forgotten": False, **usage}
        revocation_pending = False
        try:
            self.dynsec.delete_node_user(node_id)
        except HubError as e:
            if not force:
                self.registry.audit(actor, "node.forget", node_id, {"error": e.code}, "failed")
                raise
            revocation_pending = True
            log.warning("forget %s: broker revocation failed (%s); continuing because force=true", node_id, e.message)
        eids = self.registry.soft_delete_node(node_id, revocation_pending)
        if not revocation_pending:
            time.sleep(0.3)                    # let the broker publish the kicked client's LWT, then clear it
            self.bridge.clear_retained(node_id)
        self.registry.audit(actor, "node.forget", node_id,
                            {"endpoint_ids": eids, "scenes": usage["scenes"], "force": force,
                             "revocation_pending": revocation_pending})
        self.registry.bus.publish("node_status", {"node_id": node_id, "online": False})
        self.registry.changed("forget", eids)
        return {"forgotten": True, "revocation_pending": revocation_pending, **usage}

    def _cancel_unannounced(self, node_id: str, confirm: bool, actor: str) -> dict:
        """A node that was provisioned but never announced has only a session and an MQTT user: remove both."""
        active = [x for x in self.provisioning.list_sessions(active_only=True) if x["node_id"] == node_id]
        if not active:
            raise NotFound("unknown_node", "no such node")
        usage = {"scenes": 0, "automations": 0, "scene_list": [], "message": "never announced; only a provisioning session exists"}
        if not confirm:
            return {"forgotten": False, **usage}
        self.dynsec.delete_node_user(node_id)
        self.provisioning.cancel(node_id)
        self.registry.audit(actor, "node.forget", node_id, {"session_only": True})
        return {"forgotten": True, "revocation_pending": False, **usage}

    def retry_pending_revocations(self) -> list[str]:
        done = []
        for node_id in self.registry.nodes_pending_revocation():
            try:
                self.dynsec.delete_node_user(node_id)
            except HubError:
                continue
            self.bridge.clear_retained(node_id)
            self.registry.clear_revocation_pending(node_id)
            self.registry.audit("system", "node.revoked", node_id)
            done.append(node_id)
        return done
