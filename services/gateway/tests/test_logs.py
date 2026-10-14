"""No secret or token in logs (checked at DEBUG, with a RAW handler: the redaction filter is a safety net, not the defence),
none in stdout/stderr, and none in plaintext in the database."""
from __future__ import annotations

import io
import json
import logging
import re

from gateway.mock import action_hash

LOCK = {"type": "set_state", "endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}


def test_no_secrets_in_logs_stdout_or_db_at_debug(gw, capsys):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    for n in ("httpx", "httpcore", "uvicorn", "asyncio", "multipart", "python_multipart"):
        logging.getLogger(n).setLevel(logging.DEBUG)
    secrets_ = set()
    try:
        secrets_ |= set(gw.m.passwords.values()) | {gw.m.hub_secret, gw.m.device_hub_secret, gw.m.agent.secret}
        # auth
        login = gw.pub.post("/auth/login", json={"username": "admin", "password": gw.m.passwords["admin"]}).json()
        gw.pub.post("/auth/login", json={"username": "admin", "password": "wrong-password-LEAK1"})
        secrets_ |= {login["access_token"], login["refresh_token"], "wrong-password-LEAK1"}
        t2 = gw.pub.post("/auth/refresh", json={"refresh_token": login["refresh_token"]}).json()
        gw.pub.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})            # reuse path logs/audits too
        secrets_ |= {t2["access_token"], t2["refresh_token"]}
        admin = {"Authorization": "Bearer " + t2["access_token"]}
        # provisioning with a saved profile and an inline one
        assert gw.pub.post("/admin/wifi-profiles", json={"name": "home", "ssid": "HomeNet", "password": "profile-pass-LEAK2"},
                           headers=admin).status_code == 201
        gw.pub.get("/admin/wifi-profiles", headers=admin)
        gw.pub.post("/admin/wifi-profiles", json={"name": "bad", "ssid": "x", "password": "p" * 70 + "-LEAK7"}, headers=admin)   # 400
        secrets_.add("p" * 70 + "-LEAK7")
        # websocket ticket: minted, used once in the subprotocol header, and once more in a first message (rejected)
        tk = gw.pub.post("/ws/ticket", headers=admin).json()["ticket"]
        secrets_.add(tk)
        with gw.pub.websocket_connect("/ws", subprotocols=[f"edge-ticket.{tk}"]) as s:
            s.receive_json()
        for body in ({"wifi_profile": "home"}, {"wifi": {"ssid": "x", "password": "inline-pass-LEAK3"}}):
            r = gw.pub.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddeeff", **body}, headers=admin)
            assert r.status_code == 201
            secrets_.add(r.json()["bundle"]["mqtt"]["password"])
        secrets_ |= {"profile-pass-LEAK2", "inline-pass-LEAK3"}
        # users
        gw.pub.post("/admin/users", json={"username": "zed", "password": "zed-pass-LEAK4", "role": "member"}, headers=admin)
        secrets_.add("zed-pass-LEAK4")
        # confirmation flows: direct token, scene token, agent flow, consume with right/wrong secrets
        tok = gw.pub.post("/confirmations", json={"action": LOCK}, headers=admin).json()["confirmation_token"]
        secrets_.add(tok)
        gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok}, headers=admin)
        gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok}, headers=admin)
        st = gw.pub.post("/confirmations", json={"action": {"type": "run_scene", "scene": "sc_00000002"}}, headers=admin).json()["confirmation_token"]
        secrets_.add(st)
        gw.pub.post("/scenes/sc_00000002/run", json={"confirmation_token": st}, headers=admin)
        chat = gw.pub.post("/chat", json={"text": "unlock the front door"}, headers=admin).text
        rid = re.search(r'"request_id": "([^"]+)"', chat).group(1)
        gw.pub.post("/confirmations", json={"request_id": rid}, headers=admin)
        secrets_.add(gw.m.agent.deliveries[-1]["confirmation_token"])
        gw.consume(tok, "x", action_hash(LOCK))
        gw.consume(tok, "x", action_hash(LOCK), secret=False)
        gw.int.post("/internal/confirmations/consume", json={"token": tok}, headers={"X-Service-Secret": "wrong-secret-LEAK5"})
        secrets_.add("wrong-secret-LEAK5")
        # media + voice
        gw.pub.post("/images", files={"image": ("a.png", b"\x89PNG\r\n\x1a\n" + b"IMGDATA-LEAK6" * 10, "image/png")}, headers=admin)
        secrets_.add("IMGDATA-LEAK6")
        # failure paths
        gw.m.hub.secret = "rotated"
        gw.pub.get("/devices", headers=admin)
        gw.m.agent.secret = "rotated-LEAK8"
        gw.pub.post("/chat", json={"text": "hi"}, headers=admin)                      # agent says 401 to the old secret
        secrets_.add("rotated-LEAK8")
        gw.m.agent.secret = gw.m.agent.secret
        gw.m.agent.fail_delivery = True
        gw.pub.post("/confirmations", json={"action": LOCK}, headers=admin)
    finally:
        root.removeHandler(h)
        root.setLevel(old)
    logs = buf.getvalue()
    out = capsys.readouterr()
    assert len(logs) > 0
    for s in secrets_:
        assert s not in logs, f"secret leaked into the logs: {s[:6]}..."
        assert s not in out.out + out.err
    # the database holds only hashes / ciphertext, and the audit log carries no secret
    dump = "\n".join(gw.ctx.db._c.iterdump())
    for s in secrets_ - {gw.m.hub_secret, gw.m.device_hub_secret}:
        assert s not in dump, f"plaintext secret in the DB: {s[:6]}..."
    audit = json.dumps([dict(r) for r in gw.ctx.db.all("SELECT * FROM audit_log")])
    assert all(s not in audit for s in secrets_)
