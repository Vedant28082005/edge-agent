from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gateway.db import DB
from gateway.security import hash_password, verify_password


def login(gw, name="member", pw=None):
    return gw.pub.post("/auth/login", json={"username": name, "password": pw or gw.m.passwords[name]})


def test_passwords_are_argon2_and_no_default_admin(gw):
    row = gw.ctx.db.one("SELECT pw_hash FROM users WHERE username='admin'")
    assert row["pw_hash"].startswith("$argon2id$") and gw.m.passwords["admin"] not in row["pw_hash"]
    assert verify_password(row["pw_hash"], gw.m.passwords["admin"])
    fresh = DB(":memory:")
    assert fresh.one("SELECT COUNT(*) c FROM users")["c"] == 0           # nothing is seeded outside --mock


def test_login_bad_credentials_are_indistinguishable(gw):
    a = login(gw, "member", "wrong-password")
    b = login(gw, "nobody", "wrong-password")
    c = gw.pub.post("/auth/login", json={"username": "x" * 65, "password": "p"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    assert c.status_code == 400


def test_refresh_rotates_and_old_token_is_dead(gw):
    t1 = login(gw).json()
    r = gw.pub.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"] and t2["role"] == "member"
    assert gw.pub.get("/devices", headers={"Authorization": "Bearer " + t2["access_token"]}).status_code == 200
    assert gw.pub.post("/auth/refresh", json={"refresh_token": "x" * 43}).status_code == 401


def test_refresh_reuse_revokes_the_whole_family(gw):
    t1 = login(gw).json()
    t2 = gw.pub.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    t3 = gw.pub.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).json()
    # an attacker (or a stale client) replays the first token
    assert gw.pub.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}).status_code == 401
    # ...which burned the family: even the newest, never-reused token is dead now
    assert gw.pub.post("/auth/refresh", json={"refresh_token": t3["refresh_token"]}).status_code == 401
    assert gw.pub.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    row = gw.ctx.db.one("SELECT result FROM audit_log WHERE action='auth.refresh_reuse'")
    assert row["result"] == "family_revoked"
    # a different login (new family) is unaffected
    t4 = login(gw).json()
    assert gw.pub.post("/auth/refresh", json={"refresh_token": t4["refresh_token"]}).status_code == 200


def test_refresh_expires_and_dies_with_account_changes(gw):
    t = login(gw).json()
    gw.clock.advance(gw.ctx.cfg.refresh_ttl_s + 1)
    assert gw.pub.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    t = login(gw).json()
    gw.pub.patch(f"/admin/users/{gw.uid('member')}", json={"password": "a-brand-new-password"}, headers=gw.h("admin"))
    assert gw.pub.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert login(gw, "member", "a-brand-new-password").status_code == 200


def test_access_token_expires(gw):
    h = gw.h("member")
    assert gw.pub.get("/devices", headers=h).status_code == 200
    gw.clock.advance(gw.ctx.cfg.access_ttl_s + 1)
    assert gw.pub.get("/devices", headers=h).status_code == 401


def test_token_cannot_be_forged_or_role_escalated(gw):
    import jwt as pyjwt
    tok = gw.tokens("member")["access_token"]
    claims = pyjwt.decode(tok, options={"verify_signature": False})
    forged = pyjwt.encode({**claims, "role": "admin"}, "guess", algorithm="HS256")
    assert gw.pub.get("/admin/users", headers={"Authorization": "Bearer " + forged}).status_code == 401
    none_alg = pyjwt.encode({**claims, "role": "admin"}, None, algorithm="none")
    assert gw.pub.get("/admin/users", headers={"Authorization": "Bearer " + none_alg}).status_code == 401
    assert gw.pub.get("/admin/users", headers={"Authorization": "Bearer " + tok}).status_code == 403


def test_login_rate_limit_per_user_and_ip(gw):
    mx = gw.ctx.cfg.login_max_failures
    for _ in range(mx):
        assert login(gw, "member", "bad-password-1").status_code == 401
    r = login(gw, "member", "bad-password-1")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert login(gw, "member").status_code == 429                    # even the right password is refused while limited
    gw.clock.advance(gw.ctx.cfg.login_window_s + 1)
    assert login(gw, "member").status_code == 200                    # window passed
    assert gw.ctx.db.one("SELECT 1 FROM audit_log WHERE action='auth.login' AND result='rate_limited'")


def test_login_rate_limit_is_per_ip_across_usernames(gw):
    for i in range(gw.ctx.cfg.login_max_failures_ip):
        login(gw, f"user{i}", "bad-password-1")
    assert login(gw, "member").status_code == 429


def test_success_resets_the_user_counter(gw):
    for _ in range(gw.ctx.cfg.login_max_failures - 1):
        login(gw, "member", "bad-password-1")
    assert login(gw).status_code == 200
    for _ in range(gw.ctx.cfg.login_max_failures - 1):
        assert login(gw, "member", "bad-password-1").status_code == 401


def test_audit_log_has_auth_and_admin_events_without_secrets(gw):
    login(gw, "member", "bad-password-1")
    gw.pub.post("/admin/users", json={"username": "zed", "password": "zed-password-123", "role": "guest"}, headers=gw.h("admin"))
    gw.pub.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddeeff", "wifi": {"ssid": "h", "password": "wifi-secret-xyz"}},
                headers=gw.h("admin"))
    rows = gw.ctx.db.all("SELECT action, detail, result FROM audit_log")
    actions = {r["action"] for r in rows}
    assert {"auth.login", "admin.user_create", "admin.provision_session"} <= actions
    dump = "".join(r["detail"] for r in rows)
    for secret in ("zed-password-123", "wifi-secret-xyz", gw.m.passwords["member"], "bad-password-1"):
        assert secret not in dump


def test_bootstrap_admin_cli_is_one_time_and_has_no_default(tmp_path):
    cfg = tmp_path / "gw.json"
    cfg.write_text('{"data_dir": "%s"}' % str(tmp_path / "data").replace("\\", "/"), encoding="utf-8")
    run = lambda *a, stdin="": subprocess.run([sys.executable, "-m", "gateway", "--config", str(cfg), *a], input=stdin, text=True,
                                                capture_output=True, cwd=Path(__file__).resolve().parents[1])
    short = run("bootstrap-admin", "--username", "root", "--password-stdin", stdin="short\n")
    assert short.returncode != 0 and "12 characters" in short.stderr
    ok = run("bootstrap-admin", "--username", "root", "--password-stdin", stdin="a-long-first-password\n")
    assert ok.returncode == 0, ok.stderr
    again = run("bootstrap-admin", "--username", "other", "--password-stdin", stdin="another-long-password\n")
    assert again.returncode != 0 and "one-time" in again.stderr
    assert "a-long-first-password" not in ok.stdout + ok.stderr + again.stdout + again.stderr
    db = DB(str(tmp_path / "data" / "gateway.db"))
    assert verify_password(db.one("SELECT pw_hash FROM users WHERE username='root'")["pw_hash"], "a-long-first-password")
    assert not verify_password(db.one("SELECT pw_hash FROM users WHERE username='root'")["pw_hash"], "admin")
    assert hash_password("x") != hash_password("x")                  # salted
