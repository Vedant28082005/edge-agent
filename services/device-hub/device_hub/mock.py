"""Mock mode: in-memory MQTT broker (with dynamic-security emulation and per-node ACLs), a scripted fake node, and a
fake gateway implementing /internal/confirmations/consume. No network, no Docker. Also used by the unit tests and
(FakeNode only) by the integration test against the real broker.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import queue
import re
import secrets
import threading
import time
from typing import Any, Callable

import httpx

from .contracts import VOCAB, coerce_value
from .dynsec import CONTROL_TOPIC, RESPONSE_TOPIC
from .gateway_client import GatewayClient
from .transport import MessageHandler, TransportDown
from .util import CMD_ID_RE, TOKEN_RE, Clock, SECRETS

log = logging.getLogger("device_hub.mock")


def topic_matches(flt: str, topic: str) -> bool:
    f, t = flt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


def filter_covers(acl_filter: str, sub_filter: str) -> bool:
    if acl_filter.endswith("/#"):
        return sub_filter.startswith(acl_filter[:-1]) or sub_filter == acl_filter[:-2]
    return acl_filter == sub_filter or topic_matches(acl_filter, sub_filter)


class FakeBroker:
    """Enough of Mosquitto for the hub: users/passwords, per-user ACLs from dynsec roles, retained messages, LWT,
    `$CONTROL/dynamic-security/v1` command emulation, and kicking a client when its user is deleted."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Callable[[], None] | None]" = queue.Queue()
        self._lock = threading.RLock()
        self.retained: dict[str, bytes] = {}
        self.clients: list["FakeClient"] = []
        self.users: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, list[dict]] = {}
        self.dynsec_commands: list[str] = []      # command names only (never payloads)
        self.dynsec_fail = False                  # respond with an error to every command
        self.dynsec_silent = False                # never respond (timeout path)
        self._data: dict | None = None
        self.denied_publishes: list[tuple[str, str]] = []
        self._t = threading.Thread(target=self._worker, name="fake-broker", daemon=True)
        self._t.start()

    # ---- users
    def add_user(self, username: str, password: str, superuser: bool = False, clientid: str | None = None) -> None:
        SECRETS.add(password)
        with self._lock:
            self.users[username] = {"password": password, "superuser": superuser, "roles": [], "clientid": clientid}

    def client(self, username: str, password: str, client_id: str,
               will: tuple[str, bytes | str, int, bool] | None = None) -> "FakeClient":
        return FakeClient(self, username, password, client_id, will)

    # ---- plumbing
    def _worker(self) -> None:
        while True:
            fn = self._q.get()
            try:
                if fn is None:
                    return
                fn()
            except Exception:  # noqa: BLE001
                log.exception("fake broker delivery failed")
            finally:
                self._q.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until every queued delivery (including ones triggered by handlers) has run."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.unfinished_tasks == 0:
                time.sleep(0.01)
                if self._q.unfinished_tasks == 0:
                    return
            time.sleep(0.005)
        raise TimeoutError("fake broker did not drain")

    def close(self) -> None:
        self._q.put(None)

    def _acl_ok(self, c: "FakeClient", acltype: str, topic: str) -> bool:
        u = self.users.get(c.username)
        if u is None:
            return False
        if u["superuser"]:
            return True
        for role in u["roles"]:
            for a in self.roles.get(role, []):
                if a["acltype"] == acltype and a.get("allow", True):
                    ok = filter_covers(a["topic"], topic) if acltype == "subscribePattern" else topic_matches(a["topic"], topic)
                    if ok:
                        return True
        return False

    # ---- routing
    def route(self, sender: "FakeClient | None", topic: str, payload: bytes, retain: bool) -> None:
        with self._lock:
            if sender is not None and topic.startswith("$CONTROL/"):
                if not self.users.get(sender.username, {}).get("superuser"):
                    self.denied_publishes.append((sender.username, topic))
                    return
                if topic == CONTROL_TOPIC:
                    self._q.put(lambda: self._dynsec(sender, payload))
                return
            if sender is not None and not self._acl_ok(sender, "publishClientSend", topic):
                self.denied_publishes.append((sender.username, topic))
                return
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [c for c in self.clients if c.connected and any(topic_matches(s, topic) for s in c.subs)
                       and self._acl_ok(c, "publishClientReceive", topic)]
        for c in targets:
            self._q.put(lambda c=c: c._deliver(topic, payload, False))

    def deliver_retained(self, c: "FakeClient", flt: str) -> None:
        with self._lock:
            items = [(t, p) for t, p in self.retained.items() if topic_matches(flt, t)
                     and self._acl_ok(c, "publishClientReceive", t)]
        for t, p in items:
            self._q.put(lambda t=t, p=p: c._deliver(t, p, True))

    # ---- dynamic-security emulation
    def _dynsec(self, sender: "FakeClient", payload: bytes) -> None:
        if self.dynsec_silent:
            return
        doc = json.loads(payload.decode())
        responses = []
        for cmd in doc["commands"]:
            name = cmd["command"]
            self.dynsec_commands.append(name)
            err = "forced failure" if self.dynsec_fail else self._dynsec_one(name, cmd)
            r: dict[str, Any] = {"command": name}
            if not err and getattr(self, "_data", None):
                r["data"], self._data = self._data, None
            if err:
                r["error"] = err
            if "correlationData" in cmd:
                r["correlationData"] = cmd["correlationData"]
            responses.append(r)
        out = json.dumps({"responses": responses}).encode()
        if RESPONSE_TOPIC in sender.subs_topics():
            sender._deliver(RESPONSE_TOPIC, out, False)

    def _dynsec_one(self, name: str, cmd: dict) -> str | None:
        with self._lock:
            if name == "createRole":
                if cmd["rolename"] in self.roles:
                    return "Role already exists"
                self.roles[cmd["rolename"]] = list(cmd.get("acls", []))
            elif name == "createClient":
                if cmd["username"] in self.users:
                    return "Client already exists"
                self.add_user(cmd["username"], cmd.get("password", ""), clientid=cmd.get("clientid"))
                for r in cmd.get("roles", []):
                    if r["rolename"] not in self.roles:
                        del self.users[cmd["username"]]
                        return "Role not found"
                    self.users[cmd["username"]]["roles"].append(r["rolename"])
            elif name == "setClientPassword":
                if cmd["username"] not in self.users:
                    return "Client not found"
                SECRETS.add(cmd["password"])
                self.users[cmd["username"]]["password"] = cmd["password"]
            elif name == "addClientRole":
                u = self.users.get(cmd["username"])
                if u is None:
                    return "Client not found"
                if cmd["rolename"] not in self.roles:
                    return "Role not found"
                if cmd["rolename"] in u["roles"]:
                    return "Internal error"
                u["roles"].append(cmd["rolename"])
            elif name == "addRoleACL":
                acls = self.roles.get(cmd["rolename"])
                if acls is None:
                    return "Role not found"
                if any(a["acltype"] == cmd["acltype"] and a["topic"] == cmd["topic"] for a in acls):
                    return "Internal error"
                acls.append({k: cmd[k] for k in ("acltype", "topic", "priority", "allow") if k in cmd})
            elif name == "deleteClient":
                if cmd["username"] not in self.users:
                    return "Client not found"
                del self.users[cmd["username"]]
                kicked = [c for c in self.clients if c.username == cmd["username"] and c.connected]
                for c in kicked:
                    self._q.put(c.drop)
            elif name == "deleteRole":
                if cmd["rolename"] not in self.roles:
                    return "Role not found"
                del self.roles[cmd["rolename"]]
            elif name == "getClient":
                if cmd["username"] not in self.users:
                    return "Client not found"
                self._data = {"client": {"username": cmd["username"],
                                         "roles": [{"rolename": r} for r in self.users[cmd["username"]]["roles"]]}}
            elif name == "getRole":
                if cmd["rolename"] not in self.roles:
                    return "Role not found"
                self._data = {"role": {"rolename": cmd["rolename"], "acls": list(self.roles[cmd["rolename"]])}}
            else:
                return f"unsupported command {name}"
        return None


class FakeClient:
    """Transport-compatible client of FakeBroker."""

    def __init__(self, broker: FakeBroker, username: str, password: str, client_id: str,
                 will: tuple[str, bytes | str, int, bool] | None) -> None:
        self.broker, self.username, self.password, self.client_id, self.will = broker, username, password, client_id, will
        self.connected = False
        self.connect_error: str | None = None
        self.subs: dict[str, int] = {}
        self._handlers: list[MessageHandler] = []

    def subs_topics(self) -> list[str]:
        return list(self.subs)

    def add_message_handler(self, fn: MessageHandler) -> None:
        self._handlers.append(fn)

    def start(self) -> None:
        b = self.broker
        with b._lock:
            u = b.users.get(self.username)
            if u is None or u["password"] != self.password:
                self.connect_error = "Connection Refused: not authorised."
                return
            for c in [c for c in b.clients if c.client_id == self.client_id and c.connected]:
                b._q.put(c.drop)              # same client id takes over, like a real broker
            self.connect_error = None
            self.connected = True
            b.clients.append(self)
        for flt in list(self.subs):
            b.deliver_retained(self, flt)

    def wait_connected(self, timeout: float) -> bool:
        return self.connected

    def stop(self) -> None:
        self._leave(publish_will=False)

    def drop(self) -> None:
        """Unclean disconnect (broker kicked us / network died): the will is published."""
        self._leave(publish_will=True)

    def _leave(self, publish_will: bool) -> None:
        with self.broker._lock:
            was = self.connected
            self.connected = False
            if self in self.broker.clients:
                self.broker.clients.remove(self)
        if was and publish_will and self.will:
            t, p, q, r = self.will
            self.broker.route(None, t, p if isinstance(p, bytes) else p.encode(), r)

    def publish(self, topic: str, payload: bytes | str, qos: int = 1, retain: bool = False) -> None:
        if not self.connected:
            raise TransportDown("not connected")
        self.broker.route(self, topic, payload if isinstance(payload, bytes) else payload.encode(), retain)

    def subscribe(self, topic: str, qos: int = 1) -> None:
        if self.connected and not (self.broker.users.get(self.username, {}).get("superuser")
                                   or self.broker._acl_ok(self, "subscribePattern", topic)):
            self.broker.denied_publishes.append((self.username, "SUB " + topic))
            return
        self.subs[topic] = qos
        if self.connected:
            self.broker.deliver_retained(self, topic)

    def _deliver(self, topic: str, payload: bytes, retain: bool) -> None:
        for fn in list(self._handlers):
            fn(topic, payload, retain)


# ---------------------------------------------------------------------------------------------- scripted node
class FakeNode:
    """A contract-conformant ESP32 stand-in: announce (retained), status online + LWT, state (retained), cmd handling
    with cmd_id dedup, validation against its own ranges, state publish, ack. Works over any Transport."""

    def __init__(self, announce: dict, transport: Any, ack_delay: float = 0.0) -> None:
        self.announce = announce
        self.node_id = announce["node_id"]
        self.t = transport
        self.ack_delay = ack_delay
        self.drop_acks = False          # never ack (ack_timeout path)
        self.duplicate_acks = False     # ack twice (QoS1 redelivery)
        self.lie_about_value = False    # ack a different value (the hub must not trust ack values)
        self.ignore_state = False       # apply but never publish state (verified:false path)
        self.motion_delay = 0.05        # capabilities with settle_s: ack at once, state follows after this long
        self.stall = False              # motion never reaches the target (action_settle_timeout path)
        self.stop_short = 0             # motion ends this far from the target (int caps)
        self.midway = True              # publish an intermediate position before the final one
        self.wiped = False
        self.cmds_seen: list[dict] = []
        self._acks: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()   # last 16 cmd_ids (mqtt.md)
        self.values: dict[str, dict[str, Any]] = {}
        for ep in announce["endpoints"]:
            vals = {}
            for cap in ep["capabilities"]:
                if cap["kind"] == "actuator":
                    vals[cap["id"]] = cap["boot_value"]
                else:
                    vals[cap["id"]] = {"bool": False, "int": 0, "float": 0.0, "enum": (cap.get("values") or VOCAB[cap["id"]].get("values") or [""])[0]}[cap["type"]]
            self.values[ep["local_id"]] = vals
        self._caps = {ep["local_id"]: {c["id"]: c for c in ep["capabilities"]} for ep in announce["endpoints"]}
        self.topic = f"home/v1/nodes/{self.node_id}"
        self.t.add_message_handler(self._on_message)

    @staticmethod
    def will(node_id: str) -> tuple[str, bytes, int, bool]:
        return (f"home/v1/nodes/{node_id}/status", b"offline", 1, True)

    def start(self, timeout: float = 5.0, publish_announce: bool = True) -> bool:
        self.t.start()
        if not self.t.wait_connected(timeout):
            return False
        if publish_announce:
            self.publish_announce()
        return True

    def publish_announce(self) -> None:
        """The connect lifecycle of mqtt.md, in order: status online, retained announce, subscribe cmd, state."""
        self.t.publish(f"{self.topic}/status", b"online", 1, True)
        self.t.publish(f"{self.topic}/announce", json.dumps(self.announce), 1, True)
        self.t.subscribe(f"{self.topic}/cmd", 1)
        self.publish_state()

    def reconnect(self, timeout: float = 5.0) -> bool:
        """Drop the connection and connect again; the full lifecycle (including the announce) runs on EVERY connect."""
        self.t.stop()
        return self.start(timeout)

    def publish_state(self) -> None:
        self.t.publish(f"{self.topic}/state", json.dumps({"ts": int(time.time()), "endpoints": self.values}), 1, True)

    def stop(self) -> None:
        self.t.stop()

    def set_sensor(self, local_id: str, cap: str, value: Any) -> None:
        self.values[local_id][cap] = value
        self.publish_state()

    def _ack(self, cmd_id: str, ok: bool, endpoint: str | None, values: dict, error: str | None) -> None:
        payload = json.dumps({"cmd_id": cmd_id, "ok": ok, "endpoint": endpoint, "values": values, "error": error}).encode()
        self._acks[cmd_id] = payload
        while len(self._acks) > 16:
            self._acks.popitem(last=False)
        if self.drop_acks:
            return

        def send() -> None:
            try:
                self.t.publish(f"{self.topic}/ack", payload, 1, False)
                if self.duplicate_acks:
                    self.t.publish(f"{self.topic}/ack", payload, 1, False)
            except TransportDown:
                pass

        if self.ack_delay:
            threading.Timer(self.ack_delay, send).start()
        else:
            send()

    def _on_message(self, topic: str, payload: bytes, retain: bool) -> None:
        if topic != f"{self.topic}/cmd":
            return
        cmd = json.loads(payload.decode())
        cid = cmd.get("cmd_id", "")
        if not CMD_ID_RE.match(cid):
            return
        if cid in self._acks:                      # dedup: same cmd_id is never applied twice, ack is repeated
            if not self.drop_acks:
                self.t.publish(f"{self.topic}/ack", self._acks[cid], 1, False)
            return
        self.cmds_seen.append(cmd)
        if cmd.get("cmd") == "identify":
            self._ack(cid, True, None, {}, None)
            return
        if cmd.get("cmd") == "unprovision":
            self._ack(cid, True, None, {}, None)
            self.wiped = True
            return
        ep = cmd.get("endpoint")
        if ep not in self._caps:
            self._ack(cid, False, ep, {}, "unknown_endpoint")
            return
        for k, v in cmd["set"].items():
            cap = self._caps[ep].get(k)
            if cap is None or cap["kind"] != "actuator":
                self._ack(cid, False, ep, {}, "unknown_capability")
                return
            norm = {"id": k, **{x: cap[x] for x in ("type", "min", "max", "values") if x in cap}}
            norm.setdefault("values", VOCAB[k].get("values", []))
            if "min" not in norm and "min" in VOCAB[k]:
                norm["min"], norm["max"] = VOCAB[k]["min"], VOCAB[k]["max"]
            ok, code, _ = coerce_value(norm, v)
            if not ok:
                self._ack(cid, False, ep, {}, code)
                return
        moving = {k: v for k, v in cmd["set"].items() if self._caps[ep][k].get("settle_s")}
        instant = {k: v for k, v in cmd["set"].items() if k not in moving}
        self.values[ep].update(instant)
        if instant and not self.ignore_state:
            self.publish_state()
        # ack = accepted; values echo the accepted TARGET, not the position reached (mqtt.md)
        reported = {**self.values[ep], **cmd["set"]}
        if self.lie_about_value:
            for k, v in instant.items():
                reported[k] = (not v) if isinstance(v, bool) else v
        self._ack(cid, True, ep, reported, None)
        if moving and not self.stall:
            threading.Thread(target=self._move, args=(ep, moving), daemon=True).start()

    def _move(self, ep: str, target: dict) -> None:
        try:
            if self.midway:
                time.sleep(self.motion_delay / 2)
                self.values[ep].update({k: (v // 2 if isinstance(v, int) and not isinstance(v, bool) else v)
                                        for k, v in target.items()})
                self.publish_state()
            time.sleep(self.motion_delay / 2)
            self.values[ep].update({k: (v - self.stop_short if isinstance(v, int) and not isinstance(v, bool) else v)
                                    for k, v in target.items()})
            self.publish_state()
        except TransportDown:
            pass


# ---------------------------------------------------------------------------------------------- fake gateway
class FakeGateway:
    """Implements POST /internal/confirmations/consume exactly as specified in openapi/gateway.yaml (atomic
    use-once, wrong user -> unknown without burning, hash mismatch burns). Minting stands in for POST /confirmations."""

    def __init__(self, secret: str | None = None, clock: Clock | None = None) -> None:
        self.secret = secret or secrets.token_urlsafe(32)
        SECRETS.add(self.secret)
        self.clock = clock or Clock()
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.calls: list[dict] = []              # {user_id, action_hash[:12], status} (never the token)

    def mint(self, user_id: str, action_hash: str, ttl: int = 60) -> str:
        token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        assert TOKEN_RE.match(token)
        SECRETS.add(token)
        with self._lock:
            self._tokens[hashlib.sha256(token.encode()).hexdigest()] = {
                "user": user_id, "hash": action_hash, "expires_at": self.clock.now() + ttl, "used": False}
        return token

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/internal/confirmations/consume" or request.method != "POST":
            return httpx.Response(404, json={"error": "not_found"})
        if request.headers.get("X-Service-Secret") != self.secret:
            return httpx.Response(401, json={"error": "unauthorized"})
        try:
            body = json.loads(request.content)
        except ValueError:
            return httpx.Response(400, json={"error": "bad_request"})
        if not (isinstance(body, dict) and set(body) == {"token", "user_id", "action_hash"}
                and TOKEN_RE.match(str(body["token"])) and re.match(r"^[0-9a-f]{64}$", str(body["action_hash"]))
                and isinstance(body["user_id"], str) and 1 <= len(body["user_id"]) <= 64):
            return httpx.Response(400, json={"error": "bad_request"})
        key = hashlib.sha256(body["token"].encode()).hexdigest()
        now = self.clock.now()
        with self._lock:
            row = self._tokens.get(key)
            if row is None or row["user"] != body["user_id"]:
                status, reason = 403, "unknown"
            elif not row["used"] and row["expires_at"] > now:
                row["used"] = True                                   # the atomic UPDATE ... WHERE used=0
                if row["hash"] == body["action_hash"]:
                    status, reason = 200, None
                else:
                    status, reason = 403, "hash_mismatch"            # burned
            else:
                status, reason = 403, ("used" if row["used"] else "expired")
        self.calls.append({"user_id": body["user_id"], "hash": body["action_hash"][:12], "status": status})
        if status == 200:
            return httpx.Response(200, json={"consumed": True, "action_hash": body["action_hash"], "user_id": body["user_id"]})
        return httpx.Response(403, json={"consumed": False, "reason": reason})

    def client(self) -> GatewayClient:
        return GatewayClient("http://gateway-internal", self.secret, 2.0, transport=httpx.MockTransport(self.handler))

    def serve_http(self) -> tuple[Any, str]:
        """Serve the same handler on a real loopback socket (stdlib server). Returns (server, base_url)."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        gw = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                req = httpx.Request("POST", f"http://x{self.path}", headers=dict(self.headers), content=self.rfile.read(n))
                r = gw.handler(req)
                self.send_response(r.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(r.content)))
                self.end_headers()
                self.wfile.write(r.content)

            def log_message(self, *a: Any) -> None:  # noqa: ANN002, ANN401
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------------------------- mock mode wiring
def demo_announces() -> list[dict]:
    def cap(id_: str, **kw: Any) -> dict:
        spec = VOCAB[id_]
        return {"id": id_, "kind": spec["kind"], "type": spec["type"], **kw}

    return [
        {"schema": 2, "node_id": "esp32-aabbccddee01", "hw": "esp32-devkit", "fw": "0.1.0", "endpoints": [
            {"local_id": "l1", "type": "light", "capabilities": [cap("power", boot_value=False, failsafe_value=False),
                                                                cap("brightness", min=0, max=100, boot_value=50)]},
            {"local_id": "t1", "type": "sensor", "capabilities": [cap("temperature", unit="C", interval_s=30)]}]},
        {"schema": 2, "node_id": "esp32-aabbccddee02", "hw": "esp32-4relay", "fw": "0.1.0", "endpoints": [
            {"local_id": "f1", "type": "fan", "capabilities": [cap("power", boot_value=False),
                                                              cap("speed", min=1, max=5, boot_value=1)]},
            {"local_id": "d1", "type": "lock", "capabilities": [cap("lock", values=["locked", "unlocked"], boot_value="locked")]}]},
    ]


def build_mock_hub(db_path: str = ":memory:", http_port: int = 8100, seed: bool = True, clock: Clock | None = None):
    """Hub on an in-memory broker + fake gateway, optionally seeded with two provisioned+approved fake nodes."""
    from .config import HubConfig
    from .hub import Hub

    cfg = HubConfig(db_path=db_path)
    cfg.provision.broker_host = "192.0.2.10"
    cfg.provision.ca_file = ""
    cfg.http.port = http_port
    broker = FakeBroker()
    SECRETS.add("mock-hub-password")
    broker.add_user("hub-admin", "mock-hub-password", superuser=True)
    transport = broker.client("hub-admin", "mock-hub-password", "device-hub")
    gw = FakeGateway(clock=clock)
    hub = Hub(cfg, transport, gw.client(), clock=clock)
    hub.provisioning._ca_pem = lambda: "-----BEGIN CERTIFICATE-----\nMOCK\n-----END CERTIFICATE-----\n"  # type: ignore[method-assign]
    hub.start()
    nodes: list[FakeNode] = []
    if seed:
        for ann in demo_announces():
            sess = hub.admin.create_provision_session(
                {"node_id": ann["node_id"], "wifi": {"ssid": "mock", "password": "mock-wifi-pass"}}, "mock")
            m = sess["bundle"]["mqtt"]
            node = FakeNode(ann, broker.client(m["username"], m["password"], ann["node_id"], FakeNode.will(ann["node_id"])))
            node.start()
            nodes.append(node)
        broker.flush()
        names = {"l1": ("Ceiling light", "Bedroom"), "t1": ("Temp sensor", "Bedroom"), "f1": ("Fan", "Bedroom"),
                 "d1": ("Front door", "Hall")}
        for ann in demo_announces():
            hub.admin.approve_node(ann["node_id"], {"endpoints": [
                {"local_id": ep["local_id"], "name": names[ep["local_id"]][0], "room": names[ep["local_id"]][1],
                 **({"risk": "confirm"} if ep["type"] == "lock" else {})} for ep in ann["endpoints"]]}, "mock")
    return hub, broker, gw, nodes
