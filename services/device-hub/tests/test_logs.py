"""No secret may appear in logs (captured at DEBUG across the whole run), the audit log or the DB."""
from __future__ import annotations

import logging

import httpx

from conftest import WIFI, light_ep, lock_ep, make_announce
from device_hub.mock import FakeNode
from device_hub.util import SECRETS, redact, set_state_action
from test_api import Served

U = "user:vedant"
N = "esp32-000000000007"


def test_secrets_never_reach_logs_or_storage(env, caplog, capfd):
    caplog.set_level(logging.DEBUG)          # root logger at DEBUG: everything any module emits
    # 1) provisioning (MQTT password + Wi-Fi password in the bundle)
    sess = env.hub.admin.create_provision_session({"node_id": N, "wifi": WIFI}, "admin1")
    m = sess["bundle"]["mqtt"]
    node = FakeNode(make_announce(N, [light_ep("l1"), lock_ep("d1")]), env.broker.client(m["username"], m["password"], N, FakeNode.will(N)))
    node.start()
    env.broker.flush()
    env.hub.admin.approve_node(N, {"endpoints": [{"local_id": "l1", "name": "Lamp", "room": "R"},
                                                 {"local_id": "d1", "name": "Door", "room": "R", "risk": "confirm"}]}, "admin1")
    lock = env.eid(N, "d1")
    # 2) confirmation tokens: valid, replayed, wrong user, wrong action, garbage; via API and via executor
    served = Served(env.hub, env.gw)
    try:
        tokens = []
        for action_value, user in (("unlocked", U), ("locked", U)):
            tokens.append(env.token(user, set_state_action(lock, "lock", action_value)))
        good, other = tokens
        body = {"capability": "lock", "value": "unlocked", "user_id": U}
        served.http.post(f"/devices/{lock}/set", json={**body, "confirmation_token": good})
        served.http.post(f"/devices/{lock}/set", json={**body, "confirmation_token": good})                # replay
        served.http.post(f"/devices/{lock}/set", json={**body, "confirmation_token": other})               # mismatch
        served.http.post(f"/devices/{lock}/set", json={**body, "user_id": "user:eve", "confirmation_token": good})
        served.http.post(f"/devices/{lock}/set", json={**body, "confirmation_token": "not-a-token"})
        env.hub.executor.gateway = env.gw.client()
        env.gw.secret = "rotated-secret-xyz"                                                               # 401 path
        env.hub.executor.set_state(lock, "lock", "locked", user_id=U, token=other)
        # 3) failure paths that stringify exceptions
        env.broker.dynsec_fail = True
        try:
            env.hub.admin.create_provision_session({"node_id": "esp32-000000000008", "wifi": WIFI}, "admin1")
        except Exception:  # noqa: BLE001
            pass
        env.broker.dynsec_fail = False
        env.hub.admin.forget_node(N, confirm=True, force=False, actor="admin1")
        tokens_all = tokens + ["not-a-token"]
    finally:
        served.stop()

    secrets_ = [m["password"], WIFI["password"], "hub-pass-xyz", env.gw.secret, "rotated-secret-xyz", *tokens_all]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    out = capfd.readouterr()
    everything = logged + out.out + out.err
    for s in secrets_:
        if s == "not-a-token":
            continue
        assert s not in everything, f"secret leaked into logs: {s[:4]}..."
        assert s not in "\n".join(env.hub.db.conn.iterdump())
    assert any("provision session" in r.getMessage() for r in caplog.records), "logging must actually be exercised"


def test_redactor_scrubs_registered_secrets_and_key_value_shapes(caplog):
    caplog.set_level(logging.DEBUG)
    SECRETS.add("super-secret-value-123")
    logging.getLogger("x").warning("oops %s and password=hunter2hunter2 and confirmation_token: AbCdEf1234567890", "super-secret-value-123")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "super-secret-value-123" not in text and "hunter2hunter2" not in text and "AbCdEf1234567890" not in text
    assert "[REDACTED]" in text
    assert redact("X-Service-Secret=abcdef123456") == "X-Service-Secret=[REDACTED]"


def test_paho_debug_logging_is_never_enabled():
    import inspect
    from device_hub import transport
    assert "enable_logger" not in inspect.getsource(transport).replace("never call enable_logger()", "")
