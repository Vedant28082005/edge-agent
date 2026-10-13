"""Mosquitto dynamic-security client over the MQTT API ($CONTROL/dynamic-security/v1) on the hub's TLS connection.

CLAUDE.md rule 8: user/role management goes through this MQTT API, never through process arguments. Passwords exist
only in memory and in the MQTT payload, and are registered with the log redactor.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid

from .errors import HubError
from .transport import Transport, TransportDown
from .util import NODE_ID_RE, SECRETS

log = logging.getLogger("device_hub.dynsec")

CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
RESPONSE_TOPIC = "$CONTROL/dynamic-security/v1/response"


class DynsecError(HubError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__("dynsec_failed", message, status)


def node_role_name(node_id: str) -> str:
    return f"node-{node_id}"


def node_role_acls(node_id: str) -> list[dict]:
    """Same ACLs as deploy/mosquitto/node-role.template.json (a test keeps them in sync): the node may publish,
    receive, subscribe and unsubscribe only on its own subtree. Literal per-node role (mqtt.md explains why not %u)."""
    topic = f"home/v1/nodes/{node_id}/#"
    return [{"acltype": t, "topic": topic, "priority": 0, "allow": True}
            for t in ("publishClientSend", "publishClientReceive", "subscribePattern", "unsubscribePattern")]


class _Waiter:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.items: list[dict] = []
        self.event = threading.Event()


class DynsecClient:
    def __init__(self, transport: Transport, timeout_s: float = 5.0) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._waiters: dict[str, _Waiter] = {}
        transport.add_message_handler(self._on_message)
        transport.subscribe(RESPONSE_TOPIC, 1)

    def _on_message(self, topic: str, payload: bytes, retain: bool) -> None:
        if topic != RESPONSE_TOPIC:
            return
        try:
            doc = json.loads(payload.decode("utf-8"))
        except ValueError:
            log.warning("unparseable dynsec response")
            return
        for item in doc.get("responses", []):
            with self._lock:
                w = self._waiters.get(item.get("correlationData"))
                if w:
                    w.items.append(item)
                    if len(w.items) >= w.expected:
                        w.event.set()

    def _run(self, commands: list[dict]) -> list[dict]:
        """Send commands (one MQTT message, executed in order by the broker); return their response items."""
        cid = uuid.uuid4().hex
        for c in commands:
            c["correlationData"] = cid
        waiter = _Waiter(len(commands))
        with self._lock:
            self._waiters[cid] = waiter
        try:
            try:
                self.transport.publish(CONTROL_TOPIC, json.dumps({"commands": commands}), qos=1)
            except TransportDown as e:
                raise DynsecError(f"broker unreachable: {e}", 503) from None
            if not waiter.event.wait(self.timeout_s):
                raise DynsecError("no response from the dynamic-security plugin (timeout)", 504)
            return list(waiter.items)
        finally:
            with self._lock:
                self._waiters.pop(cid, None)

    @staticmethod
    def _err(item: dict) -> str | None:
        return item.get("error")

    @staticmethod
    def _already(e: str | None) -> bool:
        return bool(e) and "already" in e.lower()

    def create_node_user(self, node_id: str, password: str) -> str:
        """Idempotent (CLAUDE.md rule 8): create the node's MQTT user and per-node role; if the client already exists
        its password is SET to the new one (never kept, never an error) and the role and its ACLs are ensured.
        The credentials returned to the caller are always the ones that now work. Returns the role name."""
        if not NODE_ID_RE.match(node_id):
            raise DynsecError("invalid node_id", 400)
        SECRETS.add(password, permanent=False)
        role = node_role_name(node_id)
        acls = node_role_acls(node_id)
        # step 1: create what is missing (role with its ACLs, client without roles)
        r_role, r_client = self._run([
            {"command": "createRole", "rolename": role, "textdescription": f"ESP32 node {node_id}", "acls": acls},
            {"command": "createClient", "username": node_id, "password": password, "clientid": node_id,
             "textdescription": "provisioned node"}])
        role_existed, client_existed = self._already(self._err(r_role)), self._already(self._err(r_client))
        for r, existed in ((r_role, role_existed), (r_client, client_existed)):
            if self._err(r) and not existed:
                raise DynsecError(f"{r.get('command')} failed: {str(self._err(r))[:120]}")
        created_client = not client_existed
        # step 2: ensure the rest
        cmds: list[dict] = []
        if client_existed:
            cmds.append({"command": "setClientPassword", "username": node_id, "password": password})
        if role_existed:
            cmds += [{"command": "addRoleACL", "rolename": role, **a} for a in acls]
        cmds.append({"command": "addClientRole", "username": node_id, "rolename": role, "priority": 1})
        try:
            results = self._run(cmds)
            for c, r in zip(cmds, results):
                e = self._err(r)
                if e and not (c["command"] in ("addRoleACL", "addClientRole") and self._already(e)):
                    raise DynsecError(f"{c['command']} failed: {str(e)[:120]}")
        except DynsecError:
            if created_client:            # do not leave a half-made user behind
                try:
                    self.delete_node_user(node_id)
                except DynsecError:
                    pass
            raise
        log.info("dynsec: user+role ready for %s (%s)", node_id, "password reset" if client_existed else "created")
        return role

    def delete_node_user(self, node_id: str) -> None:
        """Delete user then role (real revocation; the broker disconnects the client). 'not found' is success."""
        if not NODE_ID_RE.match(node_id):
            raise DynsecError("invalid node_id", 400)
        res = self._run([{"command": "deleteClient", "username": node_id},
                         {"command": "deleteRole", "rolename": node_role_name(node_id)}])
        for r in res:
            e = self._err(r)
            if e and "not found" not in e.lower():
                raise DynsecError(f"deleting {r.get('command')} failed: {e[:120]}")
        log.info("dynsec: deleted user+role for %s", node_id)

    def client_exists(self, node_id: str) -> bool:
        res = self._run([{"command": "getClient", "username": node_id}])
        e = self._err(res[0])
        if e and "not found" in e.lower():
            return False
        if e:
            raise DynsecError(e[:120])
        return True
