"""Integration against the dev broker (deploy/compose.yml + compose.dev.yml). Run via run-integration.ps1, which
creates the node users (dynsec-node-user.ps1, dev helper) and exports:
  SIM_IT_NODES   comma list of node ids that have users          SIM_MQTT_CA          CA certificate
  SIM_MQTT_CREDS_DIR  folder with <node_id>.cred                  SIM_IT_HUB_ENV       deploy/mosquitto/.env (hub admin)
Nothing here prints or logs a password."""
import json
import os
import ssl
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.packettypes import PacketTypes  # noqa: F401

from simesp32.clock import Clock
from simesp32.config import _node
from simesp32.creds import credentials_for, read_cred_file
from simesp32.node import Node
from simesp32.schemas import Validator
from simesp32.transport import PahoTransport

pytestmark = pytest.mark.integration

HOST, PORT = os.environ.get("SIM_MQTT_HOST", "localhost"), int(os.environ.get("SIM_MQTT_PORT", "8883"))


def env_nodes():
    ids = [i for i in os.environ.get("SIM_IT_NODES", "").split(",") if i]
    if len(ids) < 3:
        pytest.skip("run via run-integration.ps1 (needs 3 node users on the dev broker)")
    return ids


def tls_ctx():
    ctx = ssl.create_default_context(cafile=os.environ["SIM_MQTT_CA"])
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class Observer:
    """The hub's view: privileged account subscribed to home/v1/#."""

    def __init__(self):
        kv = {}
        for line in Path(os.environ["SIM_IT_HUB_ENV"]).read_text(encoding="ascii").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v
        self.msgs = []
        self.lock = threading.Lock()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-it-hub", protocol=mqtt.MQTTv311)
        c.tls_set_context(tls_ctx())
        c.username_pw_set(kv["HUB_ADMIN_USER"], kv["HUB_ADMIN_PASSWORD"])
        c.on_message = lambda _c, _u, m: self._on(m)
        c.connect(HOST, PORT, 30)
        c.subscribe("home/v1/#", 1)
        c.loop_start()
        self.c = c

    def _on(self, m):
        with self.lock:
            self.msgs.append((m.topic, bytes(m.payload), m.retain))

    def topic(self, suffix):
        with self.lock:
            return [(p, r) for t, p, r in self.msgs if t == suffix]

    def wait(self, suffix, pred=lambda p: True, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            for p, r in self.topic(suffix):
                if pred(p):
                    return p
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting for {suffix}; saw {[t for t, _, _ in self.msgs][-8:]}")

    def cmd(self, node_id, body):
        self.c.publish(f"home/v1/nodes/{node_id}/cmd", json.dumps(body), qos=1)

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


YAML = """
node_id: %s
failsafe_after_s: 60
heartbeat_s: 30
endpoints:
  - {local_id: l1, type: light, capabilities: [{id: power, boot_value: false}, {id: brightness, boot_value: 10}]}
  - {local_id: t1, type: sensor, capabilities: [{id: temperature, interval_s: 2, generator: {kind: constant, value: 21.5}}]}
"""


class Runner:
    def __init__(self, node_id, extra=""):
        import yaml
        raw = yaml.safe_load(YAML % node_id)
        if extra:
            raw.update(yaml.safe_load(extra))
        cfg = _node(raw, 0)
        user, pw = credentials_for(node_id, os.environ["SIM_MQTT_CREDS_DIR"], False)
        tr = PahoTransport(node_id, HOST, PORT, os.environ["SIM_MQTT_CA"], user, pw,
                           f"home/v1/nodes/{node_id}/status")
        self.node = Node(cfg, tr, Validator(), Clock())
        self.stop = threading.Event()
        self.node.boot()
        tr.start()
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def _loop(self):
        while not self.stop.is_set():
            self.node.tick()
            time.sleep(0.05)

    def close(self):
        self.stop.set()
        self.th.join(2)
        self.node.shutdown()


@pytest.fixture(scope="module")
def hub():
    ids = env_nodes()
    o = Observer()
    yield o
    o.close()
    # clear the retained status/announce/state each node left behind (a node may write only its own subtree)
    for nid in ids:
        user, pw = read_cred_file(Path(os.environ["SIM_MQTT_CREDS_DIR"]) / f"{nid}.cred")
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=nid, protocol=mqtt.MQTTv311)
        c.tls_set_context(tls_ctx())
        c.username_pw_set(user, pw)
        c.connect(HOST, PORT, 30)
        c.loop_start()
        for suffix in ("status", "announce", "state"):
            c.publish(f"home/v1/nodes/{nid}/{suffix}", b"", qos=1, retain=True).wait_for_publish(3)
        c.loop_stop()
        c.disconnect()


def test_full_protocol_over_tls_against_real_broker(hub):
    a = env_nodes()[0]
    r = Runner(a)
    try:
        v = Validator()
        assert hub.wait(f"home/v1/nodes/{a}/status", lambda p: p == b"online")
        ann = json.loads(hub.wait(f"home/v1/nodes/{a}/announce"))
        v.validate("announce", ann)                                             # what the broker carried is contract-valid
        st = json.loads(hub.wait(f"home/v1/nodes/{a}/state"))
        v.validate("state", st)
        assert st["endpoints"]["l1"] == {"power": False, "brightness": 10}

        cid = str(uuid.uuid4())
        body = {"cmd_id": cid, "endpoint": "l1", "set": {"power": True, "brightness": 70}, "by": "user:it"}
        hub.cmd(a, body)
        ack = json.loads(hub.wait(f"home/v1/nodes/{a}/ack", lambda p: json.loads(p)["cmd_id"] == cid))
        v.validate("ack", ack)
        assert ack["ok"] and ack["values"] == {"power": True, "brightness": 70}
        hub.wait(f"home/v1/nodes/{a}/state", lambda p: json.loads(p)["endpoints"]["l1"]["brightness"] == 70)

        hub.cmd(a, body)                                                         # retry with the same cmd_id
        time.sleep(1.0)
        acks = [p for p, _ in hub.topic(f"home/v1/nodes/{a}/ack") if json.loads(p)["cmd_id"] == cid]
        assert len(acks) == 2 and r.node.stats["cmds_applied"] == 1              # replayed, not re-applied

        bad = str(uuid.uuid4())
        hub.cmd(a, {"cmd_id": bad, "endpoint": "l1", "set": {"brightness": 101}, "by": "agent"})
        e = json.loads(hub.wait(f"home/v1/nodes/{a}/ack", lambda p: json.loads(p)["cmd_id"] == bad))
        assert e["ok"] is False and e["error"] == "out_of_range"

        hub.wait(f"home/v1/nodes/{a}/state", lambda p: p != st and json.loads(p)["ts"] > st["ts"], timeout=8)  # sensor interval
        assert r.node.stats["violations"] == 0
    finally:
        r.close()
    # orderly shutdown published offline (retained)
    assert hub.topic(f"home/v1/nodes/{a}/status")[-1][0] == b"offline"


def test_lwt_fires_when_the_connection_drops_uncleanly(hub):
    b = env_nodes()[1]
    r = Runner(b, extra="faults: {flapping: {up_s: 3, down_s: 2}}")
    try:
        hub.wait(f"home/v1/nodes/{b}/status", lambda p: p == b"online")
        n_off = len(hub.topic(f"home/v1/nodes/{b}/status"))
        end = time.time() + 12
        seq = []
        while time.time() < end:
            seq = [p for p, _ in hub.topic(f"home/v1/nodes/{b}/status")]
            if b"offline" in seq[1:] and seq[-1] == b"online" and seq.count(b"online") >= 2:
                break
            time.sleep(0.1)
        assert b"offline" in seq and seq.count(b"online") >= 2, f"status sequence was {seq}"   # LWT, then back online
        assert len(hub.topic(f"home/v1/nodes/{b}/announce")) == 1                              # not re-announced
        assert n_off >= 1
    finally:
        r.close()


def test_unprovision_over_the_wire(hub):
    c = env_nodes()[2]
    r = Runner(c)
    try:
        hub.wait(f"home/v1/nodes/{c}/status", lambda p: p == b"online")
        cid = str(uuid.uuid4())
        hub.cmd(c, {"cmd_id": cid, "cmd": "unprovision"})
        ack = json.loads(hub.wait(f"home/v1/nodes/{c}/ack", lambda p: json.loads(p)["cmd_id"] == cid))
        assert ack["ok"] and ack["endpoint"] is None
        end = time.time() + 5
        while time.time() < end and not r.node.stopped:
            time.sleep(0.1)
        assert r.node.stopped
        assert hub.topic(f"home/v1/nodes/{c}/status")[-1][0] == b"offline"
    finally:
        r.stop.set()


def _v5_client(node_id):
    user, pw = read_cred_file(Path(os.environ["SIM_MQTT_CREDS_DIR"]) / f"{node_id}.cred")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=node_id, protocol=mqtt.MQTTv5)
    c.tls_set_context(tls_ctx())
    c.username_pw_set(user, pw)
    return c


def test_acl_node_may_use_own_topics_and_is_denied_on_another_nodes(hub):
    ids = env_nodes()
    a, other = ids[0], ids[1]
    c = _v5_client(a)
    results = {}
    done = threading.Event()
    c.on_publish = lambda cl, u, mid, rc, props: results.__setitem__(("pub", mid), rc)
    c.on_subscribe = lambda cl, u, mid, rcs, props: results.__setitem__(("sub", mid), rcs)
    c.connect(HOST, PORT, 30)
    c.loop_start()
    try:
        own = c.publish(f"home/v1/nodes/{a}/state", json.dumps({"probe": "own"}), qos=1)
        foreign = c.publish(f"home/v1/nodes/{other}/state", json.dumps({"probe": "foreign"}), qos=1)
        foreign_cmd = c.publish(f"home/v1/nodes/{other}/cmd", json.dumps({"probe": "forged-cmd"}), qos=1)
        perception = c.publish("home/v1/perception/jetson-yolo/events", json.dumps({"probe": "perc"}), qos=1)
        for info in (own, foreign, foreign_cmd, perception):
            info.wait_for_publish(5)
        want = {("pub", i.mid) for i in (own, foreign, foreign_cmd, perception)}
        end = time.time() + 5
        while time.time() < end and not want <= set(results):      # on_publish runs just after wait_for_publish
            time.sleep(0.05)
        _r, sub_own_mid = c.subscribe(f"home/v1/nodes/{a}/cmd", qos=1)
        _r, sub_other_mid = c.subscribe(f"home/v1/nodes/{other}/#", qos=1)
        end = time.time() + 5
        while time.time() < end and not {("sub", sub_own_mid), ("sub", sub_other_mid)} <= set(results):
            time.sleep(0.05)
    finally:
        c.loop_stop()
        c.disconnect()

    assert results[("pub", own.mid)].is_failure is False, "publish to its own topic must be accepted"
    for denied in (foreign, foreign_cmd, perception):
        assert results[("pub", denied.mid)].value == 0x87, "publish outside its subtree must get 'Not authorized'"
    assert results[("sub", sub_own_mid)][0].value in (0, 1), "subscribe to own cmd must be granted"
    assert results[("sub", sub_other_mid)][0].value == 0x87, "subscribe to another node's subtree must be denied"
    time.sleep(0.5)
    seen = b" ".join(p for t, p, _ in list(hub.msgs))
    assert b'"probe": "own"' in seen
    for leaked in (b"foreign", b"forged-cmd", b"perc"):
        assert leaked not in seen, f"hub saw a denied message ({leaked!r})"


def test_wrong_password_and_no_credentials_are_refused():
    env_nodes()
    a = env_nodes()[0]
    for user, pw in ((a, "definitely-wrong"), (None, None)):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-it-bad", protocol=mqtt.MQTTv311)
        c.tls_set_context(tls_ctx())
        if user:
            c.username_pw_set(user, pw)
        rc = {}
        c.on_connect = lambda cl, u, f, reason, p: rc.setdefault("rc", reason)
        c.connect(HOST, PORT, 10)
        c.loop_start()
        end = time.time() + 5
        while time.time() < end and "rc" not in rc:
            time.sleep(0.05)
        c.loop_stop()
        assert rc["rc"].is_failure
