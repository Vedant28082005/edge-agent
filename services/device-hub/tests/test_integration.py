"""Integration test against the real dev broker (Mosquitto 2.0.20, TLS, dynamic-security) - NOT part of the unit run
unless the broker is reachable; it is skipped (and reported as skipped) otherwise.

Broker settings (defaults match deploy/compose.dev.yml):
  EDGE_IT_HOST (localhost)  EDGE_IT_PORT (8883)  EDGE_IT_CA (deploy/mosquitto/certs/ca.crt)  EDGE_IT_ENV (deploy/mosquitto/.env)
Scenario: scripted fake node (its own TLS MQTT connection with the credentials from the provisioning bundle) announces,
appears pending, is approved, set_state round-trips with ack, a risk:confirm action needs a gateway-consumed token,
a second node cannot touch the first node's topics, forget deletes the MQTT user and the node is refused on reconnect.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from conftest import CONTRACTS, light_ep, lock_ep, make_announce, sensor_ep
from device_hub.config import HubConfig
from device_hub.errors import HubError
from device_hub.gateway_client import GatewayClient
from device_hub.hub import Hub
from device_hub.mock import FakeGateway, FakeNode
from device_hub.transport import PahoTransport
from device_hub.util import action_hash, set_state_action

pytestmark = pytest.mark.integration
REPO = CONTRACTS.parent
HOST = os.environ.get("EDGE_IT_HOST", "localhost")
PORT = int(os.environ.get("EDGE_IT_PORT", "8883"))
CA = Path(os.environ.get("EDGE_IT_CA", REPO / "deploy" / "mosquitto" / "certs" / "ca.crt"))
ENVF = Path(os.environ.get("EDGE_IT_ENV", REPO / "deploy" / "mosquitto" / ".env"))
N1 = "esp32-1a2b3c4d5e01"
N2 = "esp32-1a2b3c4d5e02"
U = "user:it-user"


def broker_up() -> str | None:
    if not (CA.is_file() and ENVF.is_file()):
        return f"dev certs/.env missing ({CA}, {ENVF}); run deploy\\mosquitto\\new-certs.ps1 and dynsec-bootstrap.ps1"
    try:
        socket.create_connection((HOST, PORT), timeout=1).close()
    except OSError:
        return f"no broker listening on {HOST}:{PORT} (docker compose -f compose.yml -f compose.dev.yml up -d)"
    return None


def wait_for(cond, timeout=8.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
        except HubError:
            v = None
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def node_transport(bundle: dict, node_id: str, ca_file: Path) -> PahoTransport:
    m = bundle["mqtt"]
    return PahoTransport(HOST, m["port"], m["username"], m["password"], node_id, ca_file=str(ca_file),
                         will=FakeNode.will(node_id))


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    why = broker_up()
    if why:
        pytest.skip(why)
    tmp = tmp_path_factory.mktemp("it")
    env = dict(line.split("=", 1) for line in ENVF.read_text().splitlines() if "=" in line)
    pw_file = tmp / "hub.pw"
    pw_file.write_text(env["HUB_ADMIN_PASSWORD"])
    fake_gw = FakeGateway()
    srv, url = fake_gw.serve_http()
    secret_file = tmp / "device-hub.secret"
    secret_file.write_text(fake_gw.secret)
    cfg = HubConfig(db_path=str(tmp / "hub.sqlite"), contracts_dir=str(CONTRACTS))
    cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.ca_file = HOST, PORT, str(CA)
    cfg.mqtt.username, cfg.mqtt.password_file = env["HUB_ADMIN_USER"], str(pw_file)
    cfg.provision.broker_host, cfg.provision.broker_port = HOST, PORT
    cfg.gateway.url, cfg.gateway.secret_file = url, str(secret_file)
    hub = Hub.from_config(cfg)
    hub.start()
    assert hub.transport.wait_connected(10), f"hub could not connect over TLS: {hub.transport.connect_error}"
    time.sleep(0.5)                         # let retained leftovers from an earlier aborted run arrive, then wipe them
    for n in (N1, N2):
        hub.dynsec.delete_node_user(n)
        hub.bridge.clear_retained(n)
        hub.registry.live.drop_node(n)
        with hub.db.tx() as c:
            c.execute("DELETE FROM capabilities WHERE endpoint_id IN (SELECT endpoint_id FROM endpoints WHERE node_id=?)", (n,))
            c.execute("DELETE FROM endpoints WHERE node_id=?", (n,))
            c.execute("DELETE FROM nodes WHERE node_id=?", (n,))
    time.sleep(0.5)
    ca_copy = tmp / "bundle-ca.pem"
    yield hub, fake_gw, ca_copy
    for n in (N1, N2):
        try:
            hub.dynsec.delete_node_user(n)
        except Exception:  # noqa: BLE001
            pass
    hub.stop()
    srv.shutdown()


def test_end_to_end_against_real_broker(stack):
    hub, fake_gw, ca_copy = stack
    # --- provisioning session: bundle is what the BLE spec expects, and its CA works for TLS
    sess = hub.admin.create_provision_session({"node_id": N1, "wifi": {"ssid": "it-net", "password": "it-wifi-pass"}}, "it-admin")
    b = sess["bundle"]
    assert b["mqtt"]["username"] == N1 and b["mqtt"]["host"] == HOST and b["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")
    ca_copy.write_text(b["ca_pem"])

    # --- the node connects with ITS OWN credentials from the bundle and announces
    ann = make_announce(N1, [light_ep("l1"), sensor_ep("t1"), lock_ep("d1")])
    node1 = FakeNode(ann, node_transport(b, N1, ca_copy))
    assert node1.start(10), f"node could not connect with the bundle credentials: {node1.t.connect_error}"
    node = wait_for(lambda: (n := hub.registry.get_node(N1)) and n["pending_endpoints"] and n, what="pending node")
    assert node["status"] == "pending" and {p["local_id"] for p in node["pending_endpoints"]} == {"l1", "t1", "d1"}
    assert hub.provisioning.get_session(sess["session_id"])["state"] == "connected"

    # --- approve and control
    hub.admin.approve_node(N1, {"endpoints": [
        {"local_id": "l1", "name": "IT lamp", "room": "IT room"}, {"local_id": "t1", "name": "IT temp", "room": "IT room"},
        {"local_id": "d1", "name": "IT door", "room": "IT room", "risk": "confirm"}]}, "it-admin")
    eid = hub.registry.get_node(N1)["endpoints"]
    lamp = next(e["endpoint_id"] for e in eid if e["local_id"] == "l1")
    door = next(e["endpoint_id"] for e in eid if e["local_id"] == "d1")
    temp = next(e["endpoint_id"] for e in eid if e["local_id"] == "t1")
    wait_for(lambda: hub.registry.get_endpoint(lamp)["online"], what="endpoint online")

    r = hub.executor.set_state(lamp, "brightness", 40)
    assert r["ok"] and r["verified"], r
    assert node1.values["l1"]["brightness"] == 40
    assert hub.executor.set_state(lamp, "brightness", 400)["error"] == "out_of_range"
    node1.set_sensor("t1", "temperature", 24.5)
    wait_for(lambda: hub.executor.read_sensor(temp, "temperature")["value"] == 24.5, what="sensor state")
    assert hub.executor.get_state(endpoint_id=lamp)["states"][0]["values"]["brightness"] == 40

    # --- risk:confirm through the real HTTP consume client
    assert hub.executor.set_state(door, "lock", "unlocked", user_id=U)["error"] == "confirmation_required"
    tok = fake_gw.mint(U, action_hash(set_state_action(door, "lock", "unlocked")))
    assert hub.executor.set_state(door, "lock", "unlocked", user_id=U, token=tok)["ok"]
    assert hub.executor.set_state(door, "lock", "locked", user_id=U, token=tok)["error"] == "confirmation_required"   # replay

    # --- ack timeout and dedup on the real wire
    node1.drop_acks = True
    hub.cfg.ack_timeout_s = 0.5
    assert hub.executor.set_state(lamp, "power", True)["error"] == "ack_timeout"
    node1.drop_acks = False

    # --- a second node: ACLs keep it inside its own subtree (real dynsec role)
    sess2 = hub.admin.create_provision_session({"node_id": N2, "wifi": {"ssid": "it-net", "password": "it-wifi-pass"}}, "it-admin")
    node2 = FakeNode(make_announce(N2, [light_ep("l1")]), node_transport(sess2["bundle"], N2, ca_copy))
    assert node2.start(10)
    wait_for(lambda: hub.registry.node_exists(N2), what="node2 announce")
    before = hub.executor.get_state(endpoint_id=lamp)["states"][0]
    node2.t.publish(f"home/v1/nodes/{N1}/state", json.dumps({"ts": 1760000999, "endpoints": {"l1": {"power": False, "brightness": 1}}}), 1, False)
    node2.t.publish(f"home/v1/nodes/{N1}/status", b"offline", 1, True)
    time.sleep(1.0)
    assert hub.executor.get_state(endpoint_id=lamp)["states"][0] == before, "node2 must not be able to write node1's topics"
    assert hub.registry.get_endpoint(lamp)["online"] is True

    # --- forget = real revocation
    usage = hub.admin.forget_node(N1, confirm=False, force=False, actor="it-admin")
    assert usage["forgotten"] is False
    out = hub.admin.forget_node(N1, confirm=True, force=False, actor="it-admin")
    assert out["forgotten"] is True
    wait_for(lambda: not node1.t.connected, what="node1 kicked by the broker")
    refused = node_transport(b, N1, ca_copy)
    refused.start()
    try:
        wait_for(lambda: refused.connect_error, timeout=8, what="CONNACK refusal")
        assert refused.connected is False and "uthori" in refused.connect_error      # "Not authorized"
    finally:
        refused.stop()
    assert not hub.registry.node_exists(N1)
    assert hub.executor.set_state(lamp, "power", True)["error"] == "unknown_endpoint"
    # retained topics were cleared: a fresh subscriber sees nothing for N1
    seen: list[str] = []
    probe = PahoTransport(HOST, PORT, hub.cfg.mqtt.username, open(hub.cfg.mqtt.password_file).read(), "it-probe", ca_file=str(CA))
    probe.add_message_handler(lambda t, p, r: seen.append(t))
    probe.subscribe(f"home/v1/nodes/{N1}/#", 1)
    probe.start()
    assert probe.wait_connected(5)
    time.sleep(1.0)
    probe.stop()
    assert seen == [], f"retained topics left behind: {seen}"
    # node2 is untouched by node1's removal
    assert hub.registry.node_exists(N2)

    # --- cleanup of node2 and TTL cleanup path against the real broker
    node2.stop()
    node1.stop()
    hub.admin.forget_node(N2, confirm=True, force=False, actor="it-admin")
    s3 = hub.admin.create_provision_session({"node_id": N1, "wifi": {"ssid": "it-net", "password": "it-wifi-pass"}}, "it-admin")
    # real clock: expire by editing the row's deadline (the TTL arithmetic is unit-tested with a manual clock)
    with hub.db.tx() as c:
        c.execute("UPDATE provision_sessions SET expires_at = 0 WHERE session_id=?", (s3["session_id"],))
    assert hub.provisioning.cleanup_expired() == [s3["session_id"]]
    bad = node_transport(s3["bundle"], N1, ca_copy)
    bad.start()
    try:
        wait_for(lambda: bad.connect_error, timeout=8, what="refusal after TTL cleanup")
    finally:
        bad.stop()
