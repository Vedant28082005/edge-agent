from __future__ import annotations

import json
import re
import threading

import pytest

from conftest import CONTRACTS
from gateway.mock import action_hash
from gateway.security import new_token, sha256_hex

LOCK = {"type": "set_state", "endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}
SCENE = {"type": "run_scene", "scene": "sc_00000002"}


def chat_request(gw, who="admin", text="please unlock the front door"):
    r = gw.pub.post("/chat", json={"text": text}, headers=gw.h(who))
    assert r.status_code == 200
    m = re.search(r"event: confirmation_request\ndata: (\{.*\})", r.text)
    assert m, r.text
    return json.loads(m.group(1))


def agent_executor_set(gw, token, user, ep="ep_00000003", cap="lock", val="unlocked"):
    """What the agent executor does after delivery: re-issue the call with the token out of band (here: hub API)."""
    return gw.m.hub.devices, gw.pub  # placeholder to keep signature obvious; real call below


def hub_set(gw, token, user_id, ep="ep_00000003", cap="lock", val="unlocked"):
    import asyncio
    import httpx

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.m.hub.app), base_url="http://hub",
                                     headers={"X-Service-Secret": gw.m.hub_secret}) as c:
            return (await c.post(f"/devices/{ep}/set", json={"capability": cap, "value": val, "user_id": user_id,
                                                              "confirmation_token": token})).json()
    return asyncio.run(go())


# ------------------------------------------------------------------ agent flow
def test_agent_flow_full_lifecycle(gw):
    ev = chat_request(gw)
    rid = ev["request_id"]
    assert gw.ctx.db.one("SELECT status FROM confirm_requests WHERE request_id=?", (rid,))["status"] == "pending"
    r = gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin"))
    assert r.status_code == 200 and r.json()["status"] == "delivered_to_agent"
    assert "confirmation_token" not in r.json() and r.json()["action_hash"] == ev["action_hash"]
    (d,) = gw.m.agent.deliveries
    assert d["decision"] == "approved" and d["user_id"] == gw.uid("admin") and d["action"] == LOCK
    assert d["role"] == "admin" and d["action_hash"] == ev["action_hash"] and set(d) == {
        "request_id", "decision", "user_id", "role", "action", "action_hash", "confirmation_token"}
    token = d["confirmation_token"]
    assert gw.ctx.db.one("SELECT token_hash FROM confirm_tokens")["token_hash"] == sha256_hex(token)   # only the hash is stored
    assert token not in json.dumps([dict(x) for x in gw.ctx.db.all("SELECT * FROM confirm_tokens")])
    # wrong user cannot spend it, and failing does not burn it
    assert hub_set(gw, token, gw.uid("member"))["error"] == "confirmation_required"
    res = hub_set(gw, token, gw.uid("admin"))
    assert res["ok"] is True and gw.m.hub.devices["ep_00000003"]["state"]["lock"] == "unlocked"
    assert hub_set(gw, token, gw.uid("admin"))["error"] == "confirmation_required"          # single use


def test_agent_flow_only_the_owner_and_never_a_guest(gw):
    rid = chat_request(gw, "member", "unlock the front door")["request_id"]
    assert gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin")).status_code == 403    # no admin override
    assert gw.pub.post("/confirmations", json={"request_id": "nope"}, headers=gw.h("member")).status_code == 404
    # member has no Hall access, so even their own request cannot be approved
    assert gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("member")).status_code == 403
    assert gw.m.agent.deliveries == []
    grid = chat_request(gw, "guest", "unlock the front door")["request_id"]
    assert gw.pub.post("/confirmations", json={"request_id": grid}, headers=gw.h("guest")).status_code == 403
    assert gw.ctx.db.one("SELECT COUNT(*) c FROM confirm_tokens")["c"] == 0


def test_request_must_pick_exactly_one_flow(gw):
    h = gw.h("admin")
    assert gw.pub.post("/confirmations", json={}, headers=h).status_code == 400
    assert gw.pub.post("/confirmations", json={"request_id": "a", "action": LOCK}, headers=h).status_code == 400
    assert gw.pub.post("/confirmations", json={"action": {**LOCK, "extra": 1}}, headers=h).status_code == 400
    assert gw.pub.post("/confirmations", json={"action": {**LOCK, "endpoint_id": "ep_ffffffff"}}, headers=h).status_code == 404


def test_decline_then_retry_creates_a_brand_new_request(gw):
    ev = chat_request(gw)
    rid = ev["request_id"]
    assert gw.pub.post(f"/confirmations/{rid}/decline", headers=gw.h("member")).status_code == 403
    assert gw.pub.post(f"/confirmations/{rid}/decline", headers=gw.h("admin")).status_code == 204
    # declined is handed to the agent too: full action + hash + role, and NO token
    assert gw.m.agent.deliveries[-1] == {"request_id": rid, "decision": "declined", "user_id": gw.uid("admin"), "role": "admin",
                                         "action": LOCK, "action_hash": action_hash(LOCK)}
    r = gw.pub.post(f"/confirmations/{rid}/decline", headers=gw.h("admin"))
    assert r.status_code == 409 and r.json()["error"] == "request_already_handled"
    r = gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin"))                            # declined stays dead
    assert r.status_code == 409 and r.json()["error"] == "request_already_handled"
    assert gw.pub.post(f"/confirmations/{rid}/decline_x", headers=gw.h("admin")).status_code == 404
    # retry: owner only, no admin override
    owner_member = chat_request(gw, "member", "unlock the front door")["request_id"]
    gw.pub.post(f"/confirmations/{owner_member}/decline", headers=gw.h("member"))
    assert gw.pub.post(f"/confirmations/{owner_member}/retry", headers=gw.h("admin")).status_code == 403
    assert gw.pub.post("/confirmations/unknown/retry", headers=gw.h("admin")).status_code == 404
    r = gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin"))
    assert r.status_code == 202
    new = r.json()["request_id"]
    assert new != rid
    row = gw.ctx.db.one("SELECT * FROM confirm_requests WHERE request_id=?", (new,))
    assert row["status"] == "pending" and row["retry_of"] == rid and row["action_hash"] == action_hash(LOCK)
    assert gw.ctx.db.one("SELECT COUNT(*) c FROM confirm_tokens")["c"] == 0                 # retry never mints or executes
    assert gw.m.hub.devices["ep_00000003"]["state"]["lock"] == "locked"
    r = gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin"))                          # old id is dead
    assert r.status_code == 409 and r.json()["error"] == "request_already_handled"
    r = gw.pub.post(f"/confirmations/{new}/retry", headers=gw.h("admin"))                          # still pending
    assert r.status_code == 409 and r.json()["error"] == "request_not_retryable"
    # the new request can be approved
    assert gw.pub.post("/confirmations", json={"request_id": new}, headers=gw.h("admin")).status_code == 200


def test_expiry_no_response_and_token_expiry(gw):
    rid = chat_request(gw)["request_id"]
    gw.clock.advance(61)
    r = gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin"))
    assert r.status_code == 409 and r.json()["error"] == "request_expired"
    assert gw.ctx.db.one("SELECT 1 FROM audit_log WHERE action='confirm.expired'")
    assert gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin")).status_code == 202   # retry after expiry
    # minted token: 60 s TTL
    tok = gw.pub.post("/confirmations", json={"action": LOCK}, headers=gw.h("admin")).json()
    assert tok["expires_at"] == int(gw.clock()) + 60
    gw.clock.advance(61)
    assert hub_set(gw, tok["confirmation_token"], gw.uid("admin"))["error"] == "confirmation_required"
    assert gw.consume(tok["confirmation_token"], gw.uid("admin"), tok["action_hash"]).json() == {"consumed": False, "reason": "expired"}


def test_stale_request_is_refused_when_the_action_changed(gw):
    ev = chat_request(gw, "admin", "open up the house")
    assert ev["action"]["type"] == "run_scene"
    gw.m.hub.scenes["sc_00000002"]["actions"].append({"target": {"endpoint_id": "ep_00000003"}, "capability": "lock", "value": "locked"})
    r = gw.pub.post("/confirmations", json={"request_id": ev["request_id"]}, headers=gw.h("admin"))
    assert r.status_code == 409 and r.json()["error"] == "action_changed"
    assert gw.m.agent.deliveries == []


def test_failed_hand_off_burns_the_token(gw):
    rid = chat_request(gw)["request_id"]
    gw.m.agent.fail_delivery = True
    assert gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin")).status_code == 502
    assert gw.ctx.db.one("SELECT used FROM confirm_tokens")["used"] == 1
    assert gw.ctx.db.one("SELECT status FROM confirm_requests WHERE request_id=?", (rid,))["status"] == "pending"
    gw.m.agent.fail_delivery = False
    assert gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin")).status_code == 200


def test_agent_supplied_hash_is_not_trusted(gw):
    gw.ctx.confirm.register_request(gw.uid("admin"), "s", {"request_id": "evil", "summary": "x", "action": LOCK,
                                                           "action_hash": "0" * 64, "expires_in_s": 60})
    r = gw.pub.post("/confirmations", json={"request_id": "evil"}, headers=gw.h("admin"))
    assert r.status_code == 409 and r.json()["error"] == "action_changed"          # the hub's own hash wins


# ------------------------------------------------------------------ direct flow
def test_direct_flow_set_state(gw):
    h = gw.h("admin")
    assert gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked"}, headers=h).status_code == 409
    r = gw.pub.post("/confirmations", json={"action": LOCK}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "minted"
    tok = r.json()["confirmation_token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", tok) and r.json()["action_hash"] == action_hash(LOCK)
    # the token is bound to the exact action
    other = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "locked", "confirmation_token": tok}, headers=h)
    assert other.status_code == 409                       # different value -> hash_mismatch -> burned
    ok = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok}, headers=h)
    assert ok.status_code == 409                          # already burned (fail closed)
    tok2 = gw.pub.post("/confirmations", json={"action": LOCK}, headers=h).json()["confirmation_token"]
    ok = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok2}, headers=h)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    replay = gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked", "confirmation_token": tok2}, headers=h)
    assert replay.status_code == 409


def test_direct_flow_scene_uses_the_hubs_hash_and_is_scene_bound(gw):
    h = gw.h("admin")
    r = gw.pub.post("/scenes/sc_00000002/run", headers=h)
    assert r.status_code == 409 and r.json()["needs_confirmation"][0]["endpoint_id"] == "ep_00000003"
    assert gw.m.hub.devices["ep_00000001"]["state"]["power"] is False                       # nothing ran
    set_tok = gw.pub.post("/confirmations", json={"action": LOCK}, headers=h).json()["confirmation_token"]
    assert gw.pub.post("/scenes/sc_00000002/run", json={"confirmation_token": set_tok}, headers=h).status_code == 409   # set token != scene
    sc = gw.pub.post("/confirmations", json={"action": SCENE}, headers=h).json()
    scene_action = {"type": "run_scene", "scene_id": "sc_00000002", "actions": [{"endpoint_id": "ep_00000003", "capability": "lock",
                                                                                "value": "unlocked"}]}
    assert sc["action_hash"] == action_hash(scene_action)
    # a scene token cannot confirm a set_state
    assert gw.pub.post("/devices/ep_00000003/set", json={"capability": "lock", "value": "unlocked",
                                                         "confirmation_token": sc["confirmation_token"]}, headers=h).status_code == 409
    sc = gw.pub.post("/confirmations", json={"action": SCENE}, headers=h).json()
    r = gw.pub.post("/scenes/sc_00000002/run", json={"confirmation_token": sc["confirmation_token"]}, headers=h)
    assert r.status_code == 200 and r.json()["executed"] is True
    assert gw.m.hub.devices["ep_00000001"]["state"]["power"] is True
    assert gw.pub.post("/scenes/sc_00000002/run", json={"confirmation_token": sc["confirmation_token"]}, headers=h).status_code == 409


def test_every_confirmation_outcome_is_audited(gw):
    rid = chat_request(gw)["request_id"]
    gw.pub.post(f"/confirmations/{rid}/decline", headers=gw.h("admin"))
    new = gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin")).json()["request_id"]
    gw.pub.post("/confirmations", json={"request_id": new}, headers=gw.h("admin"))
    acts = [r["action"] for r in gw.ctx.db.all("SELECT action FROM audit_log WHERE action LIKE 'confirm.%'")]
    assert acts[:1] == ["confirm.requested"] and {"confirm.declined", "confirm.retried", "confirm.approved"} <= set(acts)


# ------------------------------------------------------------------ internal consume
def mint_raw(gw, user, ahash, ttl=60):
    tok = gw.ctx.confirm._mint(user, ahash, None)[0]
    if ttl != 60:
        gw.ctx.db.run("UPDATE confirm_tokens SET expires_at=? WHERE token_hash=?", (gw.ctx.db.now() + ttl, sha256_hex(tok)))
    return tok


def test_consume_scenarios_from_the_contract_file(gw):
    data = json.loads((CONTRACTS / "samples" / "confirmation" / "consume-scenarios.json").read_text(encoding="utf-8"))
    assert data["scenarios"]
    for sc in data["scenarios"]:
        t0 = gw.clock()
        t = sc.get("token")
        tok = mint_raw(gw, t["user"], t["hash"], t["ttl"]) if t else new_token()      # no token minted: an unknown one
        for call in sc["calls"]:
            gw.clock.t = t0 + call["at"]
            r = gw.consume(tok, call["user"], call["hash"])
            if call["expect"] in ("granted", "ok", "consumed"):
                assert r.status_code == 200 and r.json() == {"consumed": True, "action_hash": call["hash"], "user_id": call["user"]}, \
                    f"{sc['name']}: {call}"
            else:
                assert r.status_code == 403 and r.json() == {"consumed": False, "reason": call["expect"]}, f"{sc['name']}: {call} -> {r.json()}"
        gw.clock.t = t0


def test_consume_wrong_user_does_not_burn_and_hash_mismatch_does(gw):
    h = action_hash(LOCK)
    tok = mint_raw(gw, "alice", h)
    assert gw.consume(tok, "bob", h).json() == {"consumed": False, "reason": "unknown"}
    assert gw.consume(tok, "alice", "f" * 64).json() == {"consumed": False, "reason": "hash_mismatch"}     # right user, other action: burned
    assert gw.consume(tok, "alice", h).json() == {"consumed": False, "reason": "used"}
    tok = mint_raw(gw, "alice", h)
    assert gw.consume(tok, "alice", h).status_code == 200
    assert gw.consume(tok, "alice", h).json()["reason"] == "used"
    assert gw.consume("A" * 43, "alice", h).json()["reason"] == "unknown"


def test_consume_is_atomic_under_concurrency(gw):
    h = action_hash(LOCK)
    tok = mint_raw(gw, "alice", h)
    out, barrier = [], threading.Barrier(12)

    def go():
        barrier.wait()
        out.append(gw.consume(tok, "alice", h).status_code)
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [200] + [403] * 11


def test_consume_authentication_and_validation(gw):
    h = action_hash(LOCK)
    tok = mint_raw(gw, "alice", h)
    body = {"token": tok, "user_id": "alice", "action_hash": h}
    assert gw.int.post("/internal/confirmations/consume", json=body).status_code == 401
    assert gw.int.post("/internal/confirmations/consume", json=body, headers={"X-Service-Secret": gw.m.hub_secret}).status_code == 401  # hub-owned secret
    assert gw.int.post("/internal/confirmations/consume", json=body, headers={"X-Service-Secret": ""}).status_code == 401
    for bad in ({**body, "extra": 1}, {"token": tok}, {**body, "action_hash": "ZZ"}, {**body, "token": "short"}, {**body, "user_id": ""},
                {**body, "user_id": "x" * 65}, [1]):
        assert gw.int.post("/internal/confirmations/consume", json=bad, headers=gw.secret_hdr()).status_code == 400
    assert gw.int.get("/internal/confirmations/consume", headers=gw.secret_hdr()).status_code == 405
    assert gw.int.post("/devices", headers=gw.secret_hdr()).status_code == 404            # nothing but consume on this listener
    assert gw.int.post("/auth/login", json={"username": "admin", "password": "x"}).status_code == 404
    # the token survived all of that untouched
    assert gw.consume(tok, "alice", h).status_code == 200
    # fail closed without a verifier file
    import os
    os.remove(os.path.join(gw.ctx.cfg.verifier_dir, "device-hub.sha256"))
    tok2 = mint_raw(gw, "alice", h)
    assert gw.consume(tok2, "alice", h).status_code == 401


def test_consume_attempts_are_audited_without_the_token(gw):
    h = action_hash(LOCK)
    tok = mint_raw(gw, "alice", h)
    gw.consume(tok, "alice", h)
    gw.consume(tok, "alice", h)
    gw.consume(tok, "alice", h, secret=False)
    rows = gw.ctx.db.all("SELECT * FROM audit_log WHERE action='confirm.consume'")
    assert [r["result"] for r in rows] == ["granted", "used", "unauthorized"]
    assert tok not in json.dumps([dict(r) for r in rows])


def test_hash_matches_contract_vectors(gw):
    """The fakes' canonicalisation agrees with the contract vectors (the gateway itself never hashes: the hub does)."""
    from gateway.mock import canon
    data = json.loads((CONTRACTS / "samples" / "confirmation" / "hash-vectors.json").read_text(encoding="utf-8"))
    assert len(data["vectors"]) >= 5
    for v in data["vectors"]:
        a = dict(v["input"])
        if a.get("type") == "run_scene":                       # the hub sorts the risky actions before hashing
            a["actions"] = sorted(a["actions"], key=lambda x: (x["endpoint_id"], x["capability"]))
        assert canon(a) == v["canonical"] and action_hash(a) == v["sha256"], v["name"]


def test_handoff_is_authenticated_with_the_agent_api_secret(gw):
    chat_request(gw)
    gw.pub.post("/confirmations", json={"request_id": gw.ctx.db.one("SELECT request_id FROM confirm_requests")["request_id"]},
                headers=gw.h("admin"))
    assert gw.m.agent.calls and all(c["secret_ok"] for c in gw.m.agent.calls)                # chat + hand-off
    assert any(c["path"].startswith("/v1/confirmations/") for c in gw.m.agent.calls)
    # a wrong/rotated secret: the agent says 401, the hand-off fails, the token is burned and nothing is left usable
    gw.m.agent.secret = "rotated"
    gw.ctx.confirm.register_request(gw.uid("admin"), "s", {"request_id": "r-x", "summary": "x", "action": LOCK,
                                                           "action_hash": action_hash(LOCK), "expires_in_s": 60})
    r = gw.pub.post("/confirmations", json={"request_id": "r-x"}, headers=gw.h("admin"))
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"
    assert gw.ctx.db.one("SELECT used FROM confirm_tokens WHERE request_id='r-x'")["used"] == 1


def test_handoff_fails_closed_on_any_non_202(gw):
    for status in (200, 204, 409, 500):                      # only 202 means the agent took it
        gw.m.agent.delivery_status = status
        rid = f"r-{status}"
        gw.ctx.confirm.register_request(gw.uid("admin"), "s", {"request_id": rid, "summary": "x", "action": LOCK,
                                                               "action_hash": action_hash(LOCK), "expires_in_s": 60})
        r = gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin"))
        assert r.status_code == 502, status
        assert gw.ctx.db.one("SELECT status FROM confirm_requests WHERE request_id=?", (rid,))["status"] == "pending"
    assert gw.ctx.db.one("SELECT COUNT(*) c FROM confirm_tokens WHERE used=0")["c"] == 0


def test_expired_is_delivered_to_the_agent_and_the_user(gw):
    rid = chat_request(gw, "member", "open up the house")["request_id"]            # run_scene (member lacks Hall, so only to expire)
    gw.clock.advance(61)
    asyncio_run(gw.ctx.confirm.sweep())
    d = gw.m.agent.deliveries[-1]
    assert d["request_id"] == rid and d["decision"] == "expired" and "confirmation_token" not in d and d["role"] == "member"
    assert d["action"]["type"] == "run_scene" and d["action"]["scene_id"] == "sc_00000002" and d["action_hash"] == action_hash(d["action"])
    assert gw.ctx.db.one("SELECT status FROM confirm_requests WHERE request_id=?", (rid,))["status"] == "expired"
    n = len(gw.m.agent.deliveries)
    asyncio_run(gw.ctx.confirm.sweep())
    assert len(gw.m.agent.deliveries) == n                                         # delivered once


def test_token_expiry_before_consumption_is_expired_too(gw):
    rid = chat_request(gw)["request_id"]
    assert gw.pub.post("/confirmations", json={"request_id": rid}, headers=gw.h("admin")).status_code == 200
    gw.clock.advance(61)                                                           # hub never consumed the token
    asyncio_run(gw.ctx.confirm.sweep())
    assert gw.m.agent.deliveries[-1]["decision"] == "expired"
    row = gw.ctx.db.one("SELECT status FROM confirm_requests WHERE request_id=?", (rid,))
    assert row["status"] == "expired"
    assert gw.pub.post(f"/confirmations/{rid}/retry", headers=gw.h("admin")).status_code == 202   # expired can be retried
    # a consumed token is NOT reported as expired
    rid2 = chat_request(gw)["request_id"]
    gw.pub.post("/confirmations", json={"request_id": rid2}, headers=gw.h("admin"))
    hub_set(gw, gw.m.agent.deliveries[-1]["confirmation_token"], gw.uid("admin"))
    gw.clock.advance(61)
    asyncio_run(gw.ctx.confirm.sweep())
    assert [d["decision"] for d in gw.m.agent.deliveries if d["request_id"] == rid2] == ["approved"]       # no "expired" for it
    assert gw.ctx.db.one("SELECT status FROM confirm_requests WHERE request_id=?", (rid2,))["status"] == "approved"


def test_agent_requests_must_match_the_contract_action_shapes(gw):
    reg = gw.ctx.confirm.register_request
    ok = {"request_id": "a1", "summary": "s", "action_hash": "0" * 64, "expires_in_s": 60}
    scene = {"type": "run_scene", "scene_id": "sc_00000002", "actions": [{"endpoint_id": "ep_00000003", "capability": "lock", "value": "unlocked"}]}
    assert reg("u", "s", {**ok, "action": scene}) is True
    assert reg("u", "s", {**ok, "action": scene}) is False                              # duplicate id
    for bad in ({"type": "run_scene", "scene": "sc_00000002", "needs_confirmation": []},   # the old shape
                {**scene, "extra": 1}, {**LOCK, "extra": 1}, {"type": "unlock"}, {**scene, "actions": [{"endpoint_id": "x"}]}, "str", None):
        assert reg("u", "s", {**ok, "request_id": "b" + str(id(bad)), "action": bad}) is False, bad


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
