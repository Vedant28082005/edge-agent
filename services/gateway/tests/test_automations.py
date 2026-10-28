"""Automations (contracts/automations.md v1): API roles and scope, all five triggers, cooldown, daily limit, kill switch,
risk:confirm refusal, run-time scope re-check, LLM proposals, broken rules, notifications and WebSocket delivery.

The engine is driven directly (`on_hub_event`, `tick`) on the mock hub with a fake clock; no network, no sleeping."""
from __future__ import annotations

import asyncio
import json

import pytest

from calls import RULE, make_rule
from conftest import CONTRACTS
from test_contract import SPEC, validate

LIGHT, FAN, KITCHEN, LOCK, BLINDS = "ep_00000001", "ep_00000002", "ep_00000004", "ep_00000003", "ep_00000006"


def set_rule(**over) -> dict:
    """A rule that turns the kitchen light on when the bedroom light turns on."""
    return {**RULE, "name": "Follow", "action": {"type": "set_state", "endpoint_id": KITCHEN, "capability": "power", "value": True}, **over}


def mk(gw, who="admin", **over) -> dict:
    r = gw.pub.post("/automations", json=set_rule(**over) if "action" not in over else {**RULE, **over}, headers=gw.h(who))
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(autouse=True)
def long_tokens(gw):
    gw.ctx.auth.jwt._ttl = 10 ** 9              # the fake clock jumps by hours/days; keep the test logins valid
    yield


def run(gw, coro_fn):
    async def go():
        out = await coro_fn()
        await gw.ctx.auto.idle()
        return out
    return asyncio.run(go())


def sync(gw):
    run(gw, gw.ctx.auto.resync)


def event(gw, typ: str, data: dict):
    run(gw, lambda: gw.ctx.auto.on_hub_event({"type": typ, "data": data}))


def state(gw, ep: str, **values):
    event(gw, "state", {"endpoint_id": ep, "values": values, "ts": int(gw.clock())})


def tick(gw):
    run(gw, gw.ctx.auto.tick)


def runs(gw, rule_id: str, who="admin") -> list[dict]:
    r = gw.pub.get(f"/automations/{rule_id}/runs", headers=gw.h(who))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def outcomes(gw, rule_id: str) -> list[str]:
    return [x["outcome"] for x in reversed(runs(gw, rule_id))]


def hub_sets(gw, ep: str = KITCHEN) -> list[dict]:
    return [c for c in gw.m.hub.calls if c["method"] == "POST" and c["path"] == f"/devices/{ep}/set"]


def notes(gw, who="admin", **params) -> list[dict]:
    r = gw.pub.get("/notifications", params=params, headers=gw.h(who))
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ================================================================== API: roles, scope, validation
def test_guests_get_403_everywhere_and_no_ws_messages(gw):
    rid = make_rule(gw, "admin")
    h = gw.h("guest")
    for method, url in (("GET", "/automations"), ("POST", "/automations"), ("GET", f"/automations/{rid}"), ("PUT", f"/automations/{rid}"),
                        ("DELETE", f"/automations/{rid}"), ("POST", f"/automations/{rid}/approve"), ("POST", f"/automations/{rid}/disable"),
                        ("GET", f"/automations/{rid}/runs"), ("GET", "/notifications"), ("POST", "/notifications/read")):
        assert gw.pub.request(method, url, json=RULE, headers=h).status_code == 403, (method, url)
    for method in ("GET", "PUT"):
        assert gw.pub.request(method, "/admin/automations/kill-switch", json={"automations_enabled": True}, headers=h).status_code == 403
        assert gw.pub.request(method, "/admin/automations/kill-switch", json={"automations_enabled": True}, headers=gw.h("member")).status_code == 403
    guest = gw.ctx.bc.add(gw.ctx.auth.get(gw.uid("guest")))
    gw.pub.post(f"/automations/{rid}/disable", headers=gw.h("admin"))
    gw.pub.put("/admin/automations/kill-switch", json={"automations_enabled": False}, headers=gw.h("admin"))
    assert guest.queue.empty()


def test_input_validation_and_client_cannot_set_server_fields(gw):
    for f in (CONTRACTS / "samples" / "automation-rule-input" / "invalid").glob("*.json"):
        r = gw.pub.post("/automations", json=json.loads(f.read_text(encoding="utf-8")), headers=gw.h("admin"))
        assert r.status_code == 400 and r.json()["error"] == "bad_rule", f.name
    for extra in ({"status": "approved"}, {"created_by": "user"}, {"owner": "x"}, {"enabled": True}, {"approved_by": "x"}):
        assert gw.pub.post("/automations", json={**RULE, **extra}, headers=gw.h("admin")).status_code == 400
    bad = [{"trigger": {"type": "time_of_day", "at": "25:00"}}, {"cooldown_s": 5}, {"max_runs_per_day": 0},
           {"action": {"type": "notify", "text": "x" * 121}}, {"action": {"type": "run_scene", "scene_id": "sc_1"}},
           {"trigger": {"type": "schedule", "every_s": 30}}, {"trigger": {"type": "duration_in_state", "endpoint_id": LIGHT,
                                                                           "capability": "power", "value": True, "for_s": 5}},
           {"condition": {"all": [{"type": "time_window", "after": "01:00", "before": "02:00"}] * 5}},
           {"trigger": {"type": "device_state_change", "endpoint_id": LIGHT, "capability": "power", "to": "maybe"}},    # bad_type vs registry
           {"trigger": {"type": "device_state_change", "endpoint_id": "ep_deadbeef", "capability": "power"}},           # unknown endpoint
           {"action": {"type": "set_state", "endpoint_id": BLINDS, "capability": "position", "value": 101}},          # out of range
           {"action": {"type": "set_state", "endpoint_id": KITCHEN, "capability": "power", "value": 1}}]              # int is not bool
    for b in bad:
        r = gw.pub.post("/automations", json={**RULE, **b}, headers=gw.h("admin"))
        assert r.status_code == 400 and r.json()["error"] == "bad_rule", b
    llm = json.loads((CONTRACTS / "samples" / "automation-rule-input" / "valid" / "llm-proposal.json").read_text(encoding="utf-8"))
    r = gw.pub.post("/automations", json=llm, headers=gw.h("admin"))
    assert r.status_code == 201
    validate({"$ref": "#/components/schemas/AutomationRule"}, r.json())


def test_user_rule_is_approved_llm_proposal_is_not(gw):
    r = mk(gw, "member")
    assert (r["status"], r["enabled"], r["created_by"], r["approved_by"]) == ("approved", True, "user", gw.uid("member"))
    validate({"$ref": "#/components/schemas/AutomationRule"}, r)
    p = mk(gw, "member", source="llm_proposal")
    assert (p["status"], p["enabled"], p["created_by"]) == ("proposed", False, "llm")
    assert "approved_by" not in p and p["rooms"] == ["Bedroom", "Kitchen"]
    assert [n["level"] for n in notes(gw)] == ["rule_proposed"]


def test_llm_proposal_never_runs_until_a_user_approves(gw):
    p = mk(gw, "member", source="llm_proposal")
    sync(gw)
    state(gw, LIGHT, power=True)
    assert runs(gw, p["rule_id"]) == [] and hub_sets(gw) == []
    # guests and strangers cannot approve; a member outside the rooms cannot either
    assert gw.pub.post(f"/automations/{p['rule_id']}/approve", headers=gw.h("guest")).status_code == 403
    a = gw.pub.post(f"/automations/{p['rule_id']}/approve", headers=gw.h("admin"))
    assert a.status_code == 200 and a.json()["approved_by"] == gw.uid("admin") and a.json()["enabled"] is True
    state(gw, LIGHT, power=False)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, p["rule_id"]) == ["fired"] and len(hub_sets(gw)) == 1


def test_editing_an_approved_rule_drops_back_to_proposed_and_stops_it(gw):
    r = mk(gw)
    sync(gw)
    u = gw.pub.put(f"/automations/{r['rule_id']}", json=set_rule(name="Edited"), headers=gw.h("admin"))
    assert u.status_code == 200 and u.json()["status"] == "proposed" and u.json()["enabled"] is False and "approved_by" not in u.json()
    state(gw, LIGHT, power=True)
    assert runs(gw, r["rule_id"]) == []
    gw.pub.post(f"/automations/{r['rule_id']}/disable", headers=gw.h("admin"))
    assert gw.pub.get(f"/automations/{r['rule_id']}", headers=gw.h("admin")).json()["status"] == "disabled"


def test_member_scope_on_create_view_approve_and_delete(gw):
    hall = {"trigger": {"type": "device_state_change", "endpoint_id": LOCK, "capability": "lock"}}        # Hall: not the member's room
    r = gw.pub.post("/automations", json={**RULE, **hall}, headers=gw.h("member"))
    assert r.status_code == 403 and r.json()["error"] == "out_of_scope"
    roomless = {"trigger": {"type": "time_of_day", "at": "07:00"}}                                         # no endpoint at all
    assert gw.pub.post("/automations", json={**RULE, **roomless}, headers=gw.h("member")).status_code == 403
    assert gw.pub.post("/automations", json={**RULE, **roomless}, headers=gw.h("admin")).status_code == 201
    # the admin's Hall rule is invisible to the member; a Bedroom rule is visible and approvable by an in-scope member
    hid = mk(gw, "admin", **hall, source="llm_proposal")["rule_id"]
    bid = mk(gw, "admin", source="llm_proposal")["rule_id"]
    assert gw.pub.get(f"/automations/{hid}", headers=gw.h("member")).status_code == 404
    assert hid not in [x["rule_id"] for x in gw.pub.get("/automations", headers=gw.h("member")).json()]
    assert bid in [x["rule_id"] for x in gw.pub.get("/automations", headers=gw.h("member")).json()]
    assert gw.pub.post(f"/automations/{hid}/approve", headers=gw.h("member")).status_code == 404
    assert gw.pub.post(f"/automations/{bid}/approve", headers=gw.h("member")).status_code == 200
    assert gw.pub.delete(f"/automations/{bid}", headers=gw.h("member")).status_code == 403      # members delete their own rules only
    assert gw.pub.put(f"/automations/{bid}", json=set_rule(), headers=gw.h("member")).status_code == 403
    assert gw.pub.delete(f"/automations/{bid}", headers=gw.h("admin")).status_code == 204


def test_confirm_endpoints_are_refused_at_save_and_approval(gw):
    act = {"action": {"type": "set_state", "endpoint_id": LOCK, "capability": "lock", "value": "unlocked"}}
    for who in ("admin", "member"):
        r = gw.pub.post("/automations", json={**RULE, **act}, headers=gw.h(who))
        assert r.status_code == (409 if who == "admin" else 403)
    assert gw.pub.post("/automations", json={**RULE, **act}, headers=gw.h("admin")).json()["error"] == "confirm_endpoint_not_allowed"
    # notify is fine, and a trigger may read a risk:confirm endpoint
    ok = {"trigger": {"type": "device_state_change", "endpoint_id": LOCK, "capability": "lock", "to": "unlocked"}}
    assert gw.pub.post("/automations", json={**RULE, **ok}, headers=gw.h("admin")).status_code == 201
    # a proposal that is valid today becomes forbidden when the risk level changes: approval re-checks
    p = mk(gw, "admin", source="llm_proposal")
    gw.m.hub.devices[KITCHEN]["risk"] = "confirm"
    a = gw.pub.post(f"/automations/{p['rule_id']}/approve", headers=gw.h("admin"))
    assert a.status_code == 409 and a.json()["error"] == "confirm_endpoint_not_allowed"
    assert gw.pub.get(f"/automations/{p['rule_id']}", headers=gw.h("admin")).json()["status"] == "proposed"


def test_rule_limit(gw):
    for _ in range(100):
        gw.ctx.db.run("INSERT INTO automation_rules(rule_id,name,owner,status,created_by,body,cooldown_s,max_runs_per_day,created_at,"
                      "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (f"ru_{_:08x}", "x", "u", "disabled", "user", "{}", 10, 1, 1, 1))
    r = gw.pub.post("/automations", json=RULE, headers=gw.h("admin"))
    assert r.status_code == 409 and r.json()["error"] == "rule_limit"
    assert [n["level"] for n in notes(gw)] == ["rule_limit"]


# ================================================================== engine: the five triggers
def test_trigger_device_state_change_runs_set_state_and_logs_it(gw):
    r = mk(gw, trigger={"type": "device_state_change", "endpoint_id": LIGHT, "capability": "power", "from": False, "to": True})
    rid = r["rule_id"]
    sync(gw)
    state(gw, LIGHT, power=False)                                   # no change
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["fired"]
    assert gw.m.hub.devices[KITCHEN]["state"]["power"] is True
    [call] = hub_sets(gw)
    assert call["actor"] == "household"
    row = gw.ctx.actions.page()["items"][0]
    assert (row["source"], row["user_id"], row["kind"], row["endpoint_id"], row["status"]) == \
        (f"automation:{rid}", "household", "set_state", KITCHEN, "done")
    [run_] = runs(gw, rid)
    assert run_["action_log_id"] == row["id"] and run_["action_status"] == "done"
    validate({"$ref": "#/components/schemas/AutomationRunPage"}, gw.pub.get(f"/automations/{rid}/runs", headers=gw.h("admin")).json())
    log = gw.pub.get("/admin/action-log", headers=gw.h("admin")).json()
    validate({"$ref": "#/components/schemas/ActionLogPage"}, log)
    # a different `from` does not match
    state(gw, LIGHT, power=False)
    gw.clock.advance(100)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["fired", "fired"]


def test_trigger_device_state_change_filters(gw):
    rid = mk(gw, trigger={"type": "device_state_change", "endpoint_id": LIGHT, "capability": "power", "to": False})["rule_id"]
    sync(gw)
    state(gw, LIGHT, power=True)
    state(gw, FAN, power=True)
    assert runs(gw, rid) == []
    state(gw, LIGHT, power=False)
    assert outcomes(gw, rid) == ["fired"]


def test_trigger_device_offline_after_for_s_once_per_episode(gw):
    rid = mk(gw, action={"type": "notify", "level": "warning", "text": "Bedroom light is offline"},
             trigger={"type": "device_offline", "endpoint_id": LIGHT, "for_s": 30})["rule_id"]
    sync(gw)
    event(gw, "node_status", {"node_id": "esp32-aabbccddeeff", "online": False})
    gw.clock.advance(29)
    tick(gw)
    assert runs(gw, rid) == []
    gw.clock.advance(2)
    tick(gw)
    tick(gw)
    assert outcomes(gw, rid) == ["fired"]
    n = [x for x in notes(gw) if x.get("rule_id") == rid]
    assert n and n[0]["level"] == "warning" and n[0]["text"] == "Bedroom light is offline" and n[0]["source"] == f"automation:{rid}"
    event(gw, "node_status", {"node_id": "esp32-aabbccddeeff", "online": True})
    gw.clock.advance(100)
    event(gw, "node_status", {"node_id": "esp32-aabbccddeeff", "online": False})
    gw.clock.advance(31)
    tick(gw)
    assert outcomes(gw, rid) == ["fired", "fired"]


def test_trigger_duration_in_state(gw):
    rid = mk(gw, trigger={"type": "duration_in_state", "endpoint_id": LIGHT, "capability": "power", "value": True, "for_s": 60})["rule_id"]
    sync(gw)
    state(gw, LIGHT, power=True)
    gw.clock.advance(59)
    tick(gw)
    assert runs(gw, rid) == []
    state(gw, LIGHT, power=False)                                   # leaving the state resets the timer
    state(gw, LIGHT, power=True)
    gw.clock.advance(59)
    tick(gw)
    assert runs(gw, rid) == []
    gw.clock.advance(2)
    tick(gw)
    tick(gw)
    assert outcomes(gw, rid) == ["fired"]


def test_trigger_time_of_day_with_days(gw):
    # the mock clock is Tuesday 22:13:20 UTC
    ok = mk(gw, trigger={"type": "time_of_day", "at": "22:14", "days": ["tue"]}, action={"type": "notify", "text": "a"})["rule_id"]
    no = mk(gw, trigger={"type": "time_of_day", "at": "22:14", "days": ["mon", "wed"]}, action={"type": "notify", "text": "b"})["rule_id"]
    sync(gw)
    tick(gw)
    assert runs(gw, ok) == []
    gw.clock.advance(60)                                            # 22:14:20
    tick(gw)
    tick(gw)
    assert outcomes(gw, ok) == ["fired"] and runs(gw, no) == []


def test_trigger_schedule_every_s_between(gw):
    a = mk(gw, trigger={"type": "schedule", "every_s": 120}, action={"type": "notify", "text": "tick"}, cooldown_s=10)["rule_id"]
    b = mk(gw, trigger={"type": "schedule", "every_s": 120, "between": {"from": "06:00", "to": "07:00"}},
           action={"type": "notify", "text": "morning"}, cooldown_s=10)["rule_id"]
    sync(gw)
    tick(gw)
    gw.clock.advance(119)
    tick(gw)
    assert runs(gw, a) == []
    gw.clock.advance(2)
    tick(gw)
    assert outcomes(gw, a) == ["fired"] and runs(gw, b) == []     # 22:15 is outside 06:00-07:00
    gw.clock.advance(120)
    tick(gw)
    assert outcomes(gw, a) == ["fired", "fired"]


# ================================================================== condition, cooldown, daily limit
def test_condition_device_state_and_time_window(gw):
    cond = {"all": [{"type": "device_state", "endpoint_id": FAN, "capability": "power", "op": "eq", "value": True},
                    {"type": "time_window", "after": "22:00", "before": "06:00"}]}
    rid = mk(gw, condition=cond)["rule_id"]
    sync(gw)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["skipped_condition"] and hub_sets(gw) == []
    gw.m.hub.devices[FAN]["state"]["power"] = True
    state(gw, LIGHT, power=False)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["skipped_condition", "fired"]
    gw.clock.advance(3600 * 12)                                     # midday: outside the window
    state(gw, LIGHT, power=False)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid)[-1] == "skipped_condition"


def test_cooldown_and_daily_limit(gw):
    rid = mk(gw, cooldown_s=60, max_runs_per_day=2)["rule_id"]
    sync(gw)

    def flip():
        state(gw, LIGHT, power=False)
        state(gw, LIGHT, power=True)
    flip()
    gw.clock.advance(30)
    flip()
    gw.clock.advance(31)
    flip()
    gw.clock.advance(61)
    flip()
    assert outcomes(gw, rid) == ["fired", "skipped_cooldown", "fired", "skipped_daily_limit"]
    assert len(hub_sets(gw)) == 2
    gw.clock.advance(86400)                                         # next household day
    flip()
    assert outcomes(gw, rid)[-1] == "fired"


def test_offline_target_is_skipped_not_executed(gw):
    rid = mk(gw)["rule_id"]
    gw.m.hub.devices[KITCHEN]["online"] = False
    sync(gw)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["skipped_offline"] and hub_sets(gw) == []


def test_engine_wide_set_state_cap_fails_with_rate_limited(gw):
    rid = mk(gw)["rule_id"]
    sync(gw)
    for i in range(60):
        gw.ctx.actions.record("household", "automation:ru_0000000a", "set_state", "done", endpoint_id=FAN, capability="power", value=True)
    state(gw, LIGHT, power=True)
    [x] = runs(gw, rid)
    assert (x["outcome"], x["error"]) == ("failed", "rate_limited") and hub_sets(gw) == []


# ================================================================== kill switch
def test_kill_switch_stops_everything_persists_and_is_admin_only(gw):
    n = mk(gw, trigger={"type": "device_state_change", "endpoint_id": LIGHT, "capability": "power"}, action={"type": "notify", "text": "hi"})["rule_id"]
    s = mk(gw)["rule_id"]
    sync(gw)
    admin, member = (gw.ctx.bc.add(gw.ctx.auth.get(gw.uid(x))) for x in ("admin", "member"))
    r = gw.pub.put("/admin/automations/kill-switch", json={"automations_enabled": False}, headers=gw.h("admin"))
    assert r.status_code == 200 and r.json()["automations_enabled"] is False and r.json()["changed_by"] == gw.uid("admin")
    validate({"$ref": "#/components/schemas/AutomationKillSwitch"}, r.json())
    for q in (admin, member):
        msgs = [q.queue.get_nowait() for _ in range(q.queue.qsize())]
        assert {"type": "automations_changed", "rule_id": None, "change": "kill_switch", "automations_enabled": False} in msgs
    state(gw, LIGHT, power=True)
    state(gw, LIGHT, power=False)
    assert hub_sets(gw) == [] and all(x["outcome"] == "suppressed_kill_switch" for x in runs(gw, s) + runs(gw, n))
    assert len(runs(gw, s)) == 1                                    # sampled: once per rule per hour
    assert not [x for x in notes(gw) if x["level"] == "info"]       # not even a notify
    assert [x["level"] for x in notes(gw)].count("kill_switch") == 1
    # persisted across a restart (same DB, new engine)
    from gateway.automations import Automations
    c = gw.ctx
    assert Automations(c.db, c.hub, c.auth, c.bc, c.actions, c.cfg, c.clock).enabled is False
    gw.pub.put("/admin/automations/kill-switch", json={"automations_enabled": True}, headers=gw.h("admin"))
    gw.clock.advance(100)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, s)[-1] == "fired"
    assert gw.pub.get("/admin/automations/kill-switch", headers=gw.h("admin")).json()["automations_enabled"] is True


# ================================================================== risk: confirm and scope at run time
def test_risk_confirm_target_is_never_acted_on_only_notified(gw):
    rid = mk(gw)["rule_id"]
    sync(gw)
    gw.m.hub.devices[KITCHEN]["risk"] = "confirm"                   # became risky after the rule was approved
    state(gw, LIGHT, power=True)
    [x] = runs(gw, rid)
    assert (x["outcome"], x["error"], x["action_status"]) == ("blocked_confirm", "confirmation_required", "blocked")
    assert hub_sets(gw) == []
    assert gw.ctx.db.one("SELECT COUNT(*) n FROM confirm_requests")["n"] == 0 and gw.ctx.db.one("SELECT COUNT(*) n FROM confirm_tokens")["n"] == 0
    [n] = [y for y in notes(gw) if y["level"] == "action_blocked"]
    assert n["source"] == f"automation:{rid}" and n["endpoint_id"] == KITCHEN and "confirmation" in n["text"]
    log = gw.ctx.actions.page()["items"][0]
    assert (log["source"], log["status"], log["error"]) == (f"automation:{rid}", "blocked", "confirmation_required")
    assert "confirmation_token" not in json.dumps([x, n, log])
    state(gw, LIGHT, power=False)
    gw.clock.advance(5)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["blocked_confirm", "blocked_confirm"]
    assert len([y for y in notes(gw) if y["level"] == "action_blocked"]) == 1       # one notification per cooldown, not per trigger


def test_hub_refusal_is_still_blocked_confirm_even_with_a_stale_registry_view(gw):
    """Defence in depth: the engine never carries a token, so the hub fails closed if the gateway's risk check was bypassed."""
    rid = mk(gw)["rule_id"]
    sync(gw)
    stale = asyncio.run(gw.ctx.auto.registry())
    gw.m.hub.devices[KITCHEN]["risk"] = "confirm"

    async def old_view():
        return stale
    gw.ctx.auto.registry = old_view
    state(gw, LIGHT, power=True)
    [x] = runs(gw, rid)
    assert x["outcome"] == "blocked_confirm" and gw.m.hub.devices[KITCHEN]["state"]["power"] is False
    assert all("confirmation_token" not in json.dumps(c) for c in gw.m.hub.calls)


def test_scope_is_rechecked_at_run_time_against_the_owners_current_rooms(gw):
    rid = mk(gw, "member")["rule_id"]
    sync(gw)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["fired"]
    assert gw.pub.patch(f"/admin/users/{gw.uid('member')}", json={"rooms": ["Bedroom"]}, headers=gw.h("admin")).status_code == 200   # loses Kitchen
    gw.clock.advance(100)
    state(gw, LIGHT, power=False)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid) == ["fired", "blocked_scope"] and len(hub_sets(gw)) == 1
    row = gw.ctx.actions.page()["items"][0]
    assert (row["source"], row["status"], row["error"]) == (f"automation:{rid}", "denied", "out_of_scope")
    # a disabled owner is out of scope too
    gw.pub.patch(f"/admin/users/{gw.uid('member')}", json={"rooms": ["Bedroom", "Kitchen"], "disabled": True}, headers=gw.h("admin"))
    gw.clock.advance(100)
    state(gw, LIGHT, power=False)
    state(gw, LIGHT, power=True)
    assert outcomes(gw, rid)[-1] == "blocked_scope"


# ================================================================== registry changes
def test_registry_change_breaks_rules_and_broken_rules_never_run(gw):
    rid = mk(gw)["rule_id"]
    sync(gw)
    q = gw.ctx.bc.add(gw.ctx.auth.get(gw.uid("admin")))
    del gw.m.hub.devices[KITCHEN]                                   # forgotten
    event(gw, "registry_changed", {"version": 2, "reason": "forget", "endpoint_ids": [KITCHEN]})
    r = gw.pub.get(f"/automations/{rid}", headers=gw.h("admin")).json()
    assert r["broken"] is True
    msgs = [q.queue.get_nowait() for _ in range(q.queue.qsize())]
    assert {"type": "automations_changed", "rule_id": rid, "change": "broken"} in msgs
    assert any(m["type"] == "automation_notification" and m["level"] == "rule_broken" for m in msgs)
    state(gw, LIGHT, power=True)
    assert runs(gw, rid) == []
    a = gw.pub.post(f"/automations/{rid}/approve", headers=gw.h("admin"))
    assert a.status_code == 409 and a.json()["error"] == "rule_broken"


def test_registry_change_marks_a_now_confirm_target_broken(gw):
    rid = mk(gw)["rule_id"]
    sync(gw)
    gw.m.hub.devices[KITCHEN]["risk"] = "confirm"
    event(gw, "registry_changed", {"version": 2, "reason": "patch", "endpoint_ids": [KITCHEN]})
    assert gw.pub.get(f"/automations/{rid}", headers=gw.h("admin")).json()["broken"] is True
    state(gw, LIGHT, power=True)
    assert hub_sets(gw) == [] and runs(gw, rid) == []


def test_forget_usage_counts_automations(gw):
    mk(gw)
    mk(gw, action={"type": "notify", "text": "x"})
    sync(gw)
    assert gw.ctx.auto.usage_for_node("esp32-aabbccddeeff") == 2
    assert gw.ctx.auto.usage_for_node("esp32-000000000000") == 0


# ================================================================== notifications and history
def test_notification_visibility_read_state_and_ws_shapes(gw):
    mine = mk(gw, "member", action={"type": "notify", "text": "mine"}, trigger={"type": "device_state_change", "endpoint_id": LIGHT,
                                                                                "capability": "power"})["rule_id"]
    hall = mk(gw, "admin", action={"type": "notify", "text": "hall"}, trigger={"type": "device_state_change", "endpoint_id": LOCK,
                                                                               "capability": "lock"})["rule_id"]
    sync(gw)
    ws = {x: gw.ctx.bc.add(gw.ctx.auth.get(gw.uid(x))) for x in ("admin", "member", "guest")}
    state(gw, LIGHT, power=True)
    state(gw, LOCK, lock="unlocked")
    got = {x: [q.queue.get_nowait() for _ in range(q.queue.qsize())] for x, q in ws.items()}
    assert got["guest"] == []
    for m in got["admin"] + got["member"]:
        validate({"$ref": "#/components/schemas/WsMessage"}, m)
    assert [m["text"] for m in got["member"] if m["type"] == "automation_notification"] == ["mine"]
    assert sorted(m["text"] for m in got["admin"] if m["type"] == "automation_notification") == ["hall", "mine"]
    member = notes(gw, "member")
    assert [n["text"] for n in member] == ["mine"] and member[0]["read"] is False
    validate({"$ref": "#/components/schemas/NotificationPage"}, gw.pub.get("/notifications", headers=gw.h("member")).json())
    # read state is per user
    assert gw.pub.post("/notifications/read", json={"notification_ids": [member[0]["notification_id"]]}, headers=gw.h("member")).status_code == 204
    assert notes(gw, "member", unread="true") == [] and len(notes(gw, "admin", unread="true")) == 2
    assert [n["text"] for n in notes(gw, "admin", rule_id=hall)] == ["hall"]
    assert gw.pub.get("/notifications?rule_id=nope", headers=gw.h("admin")).status_code == 400
    assert gw.pub.get(f"/notifications?limit=1", headers=gw.h("admin")).json()["next_cursor"]
    assert mine != hall


def test_run_history_survives_deletion_for_admins(gw):
    rid = mk(gw)["rule_id"]
    sync(gw)
    state(gw, LIGHT, power=True)
    assert gw.pub.delete(f"/automations/{rid}", headers=gw.h("admin")).status_code == 204
    assert gw.pub.get(f"/automations/{rid}", headers=gw.h("admin")).status_code == 404
    [x] = runs(gw, rid)
    assert x["rule_deleted"] is True and x["outcome"] == "fired"
    assert gw.pub.get(f"/automations/{rid}/runs", headers=gw.h("member")).status_code == 404


def test_run_pagination(gw):
    rid = mk(gw, cooldown_s=10, max_runs_per_day=1000)["rule_id"]
    sync(gw)
    for _ in range(5):
        gw.clock.advance(11)
        state(gw, LIGHT, power=False)
        state(gw, LIGHT, power=True)
    p1 = gw.pub.get(f"/automations/{rid}/runs?limit=2", headers=gw.h("admin")).json()
    assert len(p1["items"]) == 2 and p1["next_cursor"]
    p2 = gw.pub.get(f"/automations/{rid}/runs?limit=10&cursor={p1['next_cursor']}", headers=gw.h("admin")).json()
    assert len(p2["items"]) == 3 and p2["next_cursor"] is None and p2["items"][0]["run_id"] < p1["items"][-1]["run_id"]
    assert gw.pub.get(f"/automations/{rid}/runs?cursor=zzz", headers=gw.h("admin")).status_code == 400


def test_bare_automation_source_is_invalid_in_the_action_log(gw):
    with pytest.raises(AssertionError):
        gw.ctx.actions.record("household", "automation", "set_state", "done")
    gw.ctx.actions.record("household", "automation:ru_0a1b2c3d", "set_state", "done")


def test_samples_validate_against_what_the_gateway_emits(gw):
    for kind, schema in (("automation-rule", "AutomationRule"), ("notification", "Notification"), ("automation-run-page", "AutomationRunPage"),
                         ("automation-kill-switch", "AutomationKillSwitch")):
        for f in (CONTRACTS / "samples" / kind / "valid").glob("*.json"):
            validate({"$ref": f"#/components/schemas/{schema}"}, json.loads(f.read_text(encoding="utf-8")))
    assert SPEC["info"]["version"] == "2.13.0"


def test_hub_event_stream_feeds_the_engine(gw):
    """The real wiring: the gateway's hub subscriber (SSE) hands state events to the engine, and the engine loop ticks."""
    rid = mk(gw)["rule_id"]
    gw.m.hub.push("state", {"endpoint_id": LIGHT, "values": {"power": True}, "ts": int(gw.clock())})

    async def go():
        gw.ctx.start(0.05)
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if gw.ctx.db.one("SELECT 1 FROM automation_runs"):
                    break
        finally:
            gw.ctx.stop.set()
            for t in gw.ctx.tasks:
                t.cancel()
            await asyncio.gather(*gw.ctx.tasks, return_exceptions=True)
    asyncio.run(go())
    assert outcomes(gw, rid) == ["fired"] and gw.m.hub.devices[KITCHEN]["state"]["power"] is True
