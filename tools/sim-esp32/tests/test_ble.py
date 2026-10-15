"""S2b: BLE provisioning flow in the simulator (fake GATT peer, no radio)."""
import json
import os
import struct

import pytest

from simesp32 import provclient
from simesp32.ble import (BleBoard, BleClient, LocalPeer, Policy, ProvisionError, TcpPeer, new_pop, parse_bundle,
                          serve_board)
from simesp32.clock import ManualClock

NID = "esp32-a1b2c3d4e501"
POP = new_pop()
CA = "-----BEGIN CERTIFICATE-----\n" + "MIIBszCCAVmgAwIBAgIUQ2xyZmFrZWNh\n" * 4 + "-----END CERTIFICATE-----\n"


def bundle(user=NID):
    return {"v": 1, "wifi": {"ssid": "HomeNet", "password": "wifi-secret-pw"},
            "mqtt": {"host": "192.168.1.10", "port": 8883, "username": user, "password": "mqtt-secret-pw"},
            "ntp_host": "192.168.1.10", "ca_pem": CA}


def make(pop=POP, **pol):
    clock = ManualClock()
    b = BleBoard(NID, pop, clock, Policy(**pol))
    b.boot(False)
    return b, clock, BleClient(LocalPeer(b), pop)


def test_pop_is_high_entropy_and_unique():
    assert len(new_pop()) == 26 and new_pop() != new_pop()


def test_full_flow_stores_config_and_turns_ble_off():
    b, clock, c = make()
    info = c.info()
    assert info["node_id"] == NID and info["pop_required"] is True and info["window_left_s"] == 300
    assert POP not in json.dumps(info)
    res = c.provision(bundle(), NID, chunk=64)      # many small chunks (MTU 23-ish)
    assert res == {"state": "stored", "code": "ok"}
    cfg = b.take_stored()
    assert cfg["mqtt_user"] == NID and cfg["wifi_ssid"] == "HomeNet" and cfg["mqtt_pass"] == "mqtt-secret-pw"
    assert b.take_stored() is None
    assert not b.advertising
    assert POP not in json.dumps(b.read("result").decode())


def test_wrong_pop_fails_and_n_failures_lock_ble():
    b, clock, _ = make()
    bad = BleClient(LocalPeer(b), new_pop())
    for i in range(4):
        with pytest.raises(ProvisionError) as e:
            bad.provision(bundle(), NID)
        assert e.value.code == "bad_bundle"
        assert b.config is None
    assert b.state == "open"
    with pytest.raises(ProvisionError):
        bad.provision(bundle(), NID)
    assert b.state == "locked" and not b.advertising
    good = BleClient(LocalPeer(b), POP)
    with pytest.raises(ProvisionError):
        good.provision(bundle(), NID)               # even the right PoP is refused during the cool-down
    assert not b.button_hold()
    clock.advance(299)
    b.tick()
    assert b.state == "locked"
    clock.advance(2)
    b.tick()
    assert b.state == "open"                        # unprovisioned board reopens after the cool-down
    assert good.provision(bundle(), NID)["state"] == "stored"


def test_pop_in_the_session_write_is_rejected():
    b, _, _ = make()
    peer = LocalPeer(b)
    req = {"proto": 1, "client_pub": "A" * 43 + "=", "pop": POP}
    assert peer.write_session(json.dumps(req).encode()) is None
    assert json.loads(b.read("result"))["code"] == "bad_session"


def test_window_timeout_and_button_reenable():
    b, clock, c = make()
    clock.advance(301)
    assert json.loads(b.read("result"))["code"] == "window_closed"
    assert not b.advertising
    with pytest.raises(ProvisionError) as e:
        c.provision(bundle(), NID)
    assert e.value.code == "window_closed"
    assert b.button_hold()
    assert c.provision(bundle(), NID)["state"] == "stored"


def test_idle_transfer_is_aborted():
    b, clock, _ = make()
    p = LocalPeer(b)
    # session first
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from simesp32.ble import b64e
    pub = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert p.write_session(json.dumps({"proto": 1, "client_pub": b64e(pub), "pop": ""}).encode())
    assert p.write_data(struct.pack(">HBB", 0, 1, 4) + b"abcd")[2] == 0
    assert b.state == "receiving"
    clock.advance(31)
    b.tick()
    assert b.state == "open" and b.code == "bad_chunk"


def test_out_of_order_and_oversize_chunks():
    b, _, _ = make()
    p = LocalPeer(b)
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from simesp32.ble import b64e
    pub = X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    p.write_session(json.dumps({"proto": 1, "client_pub": b64e(pub), "pop": ""}).encode())
    assert p.write_data(struct.pack(">HBB", 1, 0, 1) + b"x")[2] == 1       # not first
    assert p.write_data(struct.pack(">HBB", 0, 1, 1) + b"x")[2] == 0
    assert p.write_data(struct.pack(">HBB", 3, 0, 1) + b"x")[2] == 1       # gap
    status = 0
    for i in range(1, 60):
        status = p.write_data(struct.pack(">HBB", i, 0, 200) + b"y" * 200)[2]
        if status:
            break
    assert status == 2


def test_bundle_for_another_node_or_with_private_key_is_rejected():
    b, _, c = make()
    with pytest.raises(ProvisionError):
        c.provision(bundle("esp32-ffffffffffff"), NID)
    assert b.config is None and b.failures == 0     # authenticated sender: not counted as a guess
    bad = bundle()
    bad["ca_pem"] = "-----BEGIN PRIVATE KEY-----\nAAAA"
    with pytest.raises(ProvisionError):
        c.provision(bad, NID)
    with pytest.raises(ValueError):
        parse_bundle(json.dumps(bundle("x")).encode(), NID)


def test_factory_reset_keeps_pop_and_reopens():
    b, _, c = make()
    c.provision(bundle(), NID)
    b.take_stored()
    b.boot(True)
    assert not b.advertising
    b.factory_reset()
    assert b.config is None and b.advertising
    assert c.info()["pop_required"] is True
    assert c.provision(bundle(), NID)["state"] == "stored"


def test_board_without_pop_refuses_to_open_unless_policy_allows():
    clock = ManualClock()
    b = BleBoard(NID, "", clock, Policy())
    assert b.boot(False) is False and not b.advertising
    b = BleBoard(NID, "", clock, Policy(require_pop=False))
    assert b.boot(False) is True
    assert BleClient(LocalPeer(b), "").provision(bundle(), NID)["state"] == "stored"


def test_tcp_fake_gatt_peer_and_provclient_cli(tmp_path, capsys):
    b, _, _ = make()
    srv, port = serve_board(b)
    try:
        assert srv.server_address[0] == "127.0.0.1"
        pop_file = tmp_path / "n.pop"
        pop_file.write_text(POP)
        bf = tmp_path / "bundle.json"
        bf.write_text(json.dumps(bundle()))
        rc = provclient.main(["--peer", f"127.0.0.1:{port}", "--pop-file", str(pop_file), "--bundle-file", str(bf)])
        out = capsys.readouterr().out
        assert rc == 0 and json.loads(out)["state"] == "stored"
        assert "secret" not in out and POP not in out
        assert b.take_stored()["mqtt_user"] == NID
    finally:
        srv.shutdown()


def test_tcp_server_refuses_non_loopback():
    b, _, _ = make()
    with pytest.raises(ValueError):
        serve_board(b, host="0.0.0.0")


def test_provclient_wrong_pop_exit_code_1(tmp_path, capsys):
    b, _, _ = make()
    srv, port = serve_board(b)
    try:
        pf = tmp_path / "w.pop"
        pf.write_text(new_pop())
        bf = tmp_path / "b.json"
        bf.write_text(json.dumps(bundle()))
        assert provclient.main(["--peer", f"127.0.0.1:{port}", "--pop-file", str(pf), "--bundle-file", str(bf)]) == 1
        assert json.loads(capsys.readouterr().out)["code"] == "bad_bundle"
    finally:
        srv.shutdown()


def test_stored_config_lets_the_node_announce_on_the_bus(validator):
    """session -> BLE payload -> node connects -> announce (retained) on the in-memory bus."""
    from conftest import Rig
    rig = Rig(validator, start=False)                # node exists but has not connected: no credentials yet
    assert rig.bus.on_topic("announce") == []
    b = BleBoard(rig.cfg.node_id, POP, rig.clock, Policy())
    b.boot(False)
    cfg = BleClient(LocalPeer(b), POP).provision(bundle(rig.cfg.node_id), rig.cfg.node_id)
    assert cfg["state"] == "stored" and b.take_stored()
    rig.tr.start()                                    # what bleprov.run_ble does after BLE-STORED
    rig.run(1)
    assert len(rig.bus.on_topic("announce")) == 1
