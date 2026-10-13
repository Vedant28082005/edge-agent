from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from device_hub.config import HubConfig  # noqa: E402
from device_hub.contracts import find_contracts_dir  # noqa: E402
from device_hub.hub import Hub  # noqa: E402
from device_hub.mock import FakeBroker, FakeGateway, FakeNode  # noqa: E402
from device_hub.util import SECRETS, ManualClock, install_log_redaction  # noqa: E402

install_log_redaction()
CONTRACTS = find_contracts_dir()
CA_PEM = "-----BEGIN CERTIFICATE-----\nTESTCA\n-----END CERTIFICATE-----\n"
WIFI = {"ssid": "home-net", "password": "wifi-pass-123"}


def cap_actuator(id_: str, type_: str, **kw):
    return {"id": id_, "kind": "actuator", "type": type_, **kw}


def cap_sensor(id_: str, type_: str, **kw):
    return {"id": id_, "kind": "sensor", "type": type_, "interval_s": kw.pop("interval_s", 30), **kw}


def make_announce(node_id: str, endpoints: list[dict], **kw) -> dict:
    return {"schema": 2, "node_id": node_id, "hw": "esp32-devkit", "fw": "0.1.0", "endpoints": endpoints, **kw}


def light_ep(local_id="l1") -> dict:
    return {"local_id": local_id, "type": "light", "capabilities": [
        cap_actuator("power", "bool", boot_value=False, failsafe_value=False),
        cap_actuator("brightness", "int", min=0, max=100, boot_value=50)]}


def fan_ep(local_id="f1") -> dict:
    return {"local_id": local_id, "type": "fan", "capabilities": [
        cap_actuator("power", "bool", boot_value=False),
        cap_actuator("speed", "int", min=1, max=5, boot_value=1),
        cap_actuator("mode", "enum", values=["auto", "eco", "turbo"], boot_value="auto")]}


def sensor_ep(local_id="t1") -> dict:
    return {"local_id": local_id, "type": "sensor", "capabilities": [cap_sensor("temperature", "float", unit="C", interval_s=30)]}


def curtain_ep(local_id="c1", settle_s=2) -> dict:
    return {"local_id": local_id, "type": "cover", "capabilities": [
        cap_actuator("position", "int", min=0, max=100, unit="%", boot_value=0, settle_s=settle_s)]}


def lock_ep(local_id="d1") -> dict:
    return {"local_id": local_id, "type": "lock", "capabilities": [
        cap_actuator("lock", "enum", values=["locked", "unlocked"], boot_value="locked")]}


class Env:
    def __init__(self) -> None:
        self.clock = ManualClock()
        self.broker = FakeBroker()
        SECRETS.add("hub-pass-xyz")
        self.broker.add_user("hub-admin", "hub-pass-xyz", superuser=True)
        self.gw = FakeGateway(clock=self.clock)
        cfg = HubConfig(db_path=":memory:", contracts_dir=str(CONTRACTS), ack_timeout_s=0.4)
        cfg.provision.broker_host = "192.0.2.10"
        transport = self.broker.client("hub-admin", "hub-pass-xyz", "device-hub")
        self.hub = Hub(cfg, transport, self.gw.client(), clock=self.clock)
        self.hub.provisioning._ca_pem = lambda: CA_PEM
        self.hub.start()
        self.nodes: dict[str, FakeNode] = {}

    # --- helpers
    def add_node(self, announce: dict, approve: dict | None = None, **node_kw) -> FakeNode:
        """Provision through the real path (dynsec user), connect a fake node with the bundle credentials, announce."""
        sess = self.hub.admin.create_provision_session({"node_id": announce["node_id"], "wifi": WIFI}, "test")
        m = sess["bundle"]["mqtt"]
        node = FakeNode(announce, self.broker.client(m["username"], m["password"], announce["node_id"],
                                                     FakeNode.will(announce["node_id"])), **node_kw)
        assert node.start()
        self.broker.flush()
        self.nodes[announce["node_id"]] = node
        if approve is not None:
            self.hub.admin.approve_node(announce["node_id"], {"endpoints": [
                {"local_id": lid, "name": n, "room": r, **({"risk": rk} if rk else {})} for lid, (n, r, rk) in approve.items()]}, "test")
        return node

    def eid(self, node_id: str, local_id: str) -> str:
        for e in self.hub.registry.get_node(node_id)["endpoints"]:
            if e["local_id"] == local_id:
                return e["endpoint_id"]
        raise KeyError(local_id)

    def token(self, user: str, action: dict, ttl: int = 60) -> str:
        from device_hub.util import action_hash
        return self.gw.mint(user, action_hash(action), ttl)

    def close(self) -> None:
        self.hub.stop()
        self.broker.close()


@pytest.fixture
def env():
    e = Env()
    yield e
    e.close()


@pytest.fixture
def home(env):
    """Bedroom light+sensor, bedroom fan, hall lock (risk: confirm). Returns env with .ids."""
    env.add_node(make_announce("esp32-000000000001", [light_ep("l1"), sensor_ep("t1")]),
                 {"l1": ("Ceiling light", "Bedroom", None), "t1": ("Temperature", "Bedroom", None)})
    env.add_node(make_announce("esp32-000000000002", [fan_ep("f1"), lock_ep("d1")]),
                 {"f1": ("Fan", "Bedroom", None), "d1": ("Front door", "Hall", "confirm")})
    env.ids = {"light": env.eid("esp32-000000000001", "l1"), "temp": env.eid("esp32-000000000001", "t1"),
               "fan": env.eid("esp32-000000000002", "f1"), "lock": env.eid("esp32-000000000002", "d1")}
    return env


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def caplog_all(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def deep(d):
    return copy.deepcopy(d)
