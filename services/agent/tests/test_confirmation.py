import asyncio
import json
import logging

from conftest import GUEST_SCOPE, SECRET, collect, make_env, names, one
from agent.policy import Action, Decision, Trigger, needs_confirmation


async def approve_when_asked(env, user="alice", approve=True, delay=0.05, mint_user=None):
    """Plays the gateway: waits for a pending request, mints a token for it (fake hub) and hands it to the agent."""
    for _ in range(200):
        ids = env.agent.confirmations.pending_ids()
        if ids:
            rid = ids[0]
            await asyncio.sleep(delay)
            if not approve:
                return env.agent.confirmations.resolve(rid, user, False)
            action = env.agent.confirmations.pending_action(rid)
            tok = env.fake.mint(mint_user or user, action)
            return env.agent.confirmations.resolve(rid, user, True, tok)
        await asyncio.sleep(0.02)
    raise AssertionError("no confirmation request appeared")


def test_policy_table():
    risky, plain = Action("set_state", {}, "confirm"), Action("set_state", {}, "normal")
    assert needs_confirmation(plain, Trigger("user", user_id="a")) is Decision.ALLOW
    assert needs_confirmation(risky, Trigger("user", user_id="a")) is Decision.CONFIRM
    assert needs_confirmation(risky, Trigger("user", user_id="g", guest=True)) is Decision.DENY
    for kind in ("rule", "perception", "proactive"):
        assert needs_confirmation(risky, Trigger(kind)) is Decision.NOTIFY_ONLY
        assert needs_confirmation(plain, Trigger(kind)) is Decision.ALLOW


async def test_risk_confirm_always_asks_and_nothing_is_sent_before_approval(env):
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    for _ in range(100):
        if env.agent.confirmations.pending_ids():
            break
        await asyncio.sleep(0.02)
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]          # nothing executed while waiting
    await approve_when_asked(env)
    ev = await task
    req = one(ev, "confirmation_request")
    assert req["summary"] == "Unlock Front door?" and req["expires_in_s"] == 2
    assert req["action"] == {"type": "set_state", "endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"}
    assert len(req["action_hash"]) == 64 and req["request_id"].startswith("cr_")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1
    meta = sets[0]["meta"]
    assert meta["user_id"] == "alice" and meta["call_id"] and meta["confirmation_token"]        # attached out of band
    assert "confirmation_token" not in sets[0]["args"]
    assert one(ev, "tool_result")["ok"] is True
    assert names(ev).index("confirmation_request") < names(ev).index("tool_call") < names(ev).index("final")


async def test_token_never_appears_in_events_logs_or_store(env, caplog):
    caplog.set_level(logging.DEBUG)
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    ap = asyncio.create_task(approve_when_asked(env))
    ev = await task
    await ap
    token = [c for c in env.fake.calls if c["tool"] == "set_state"][0]["meta"]["confirmation_token"]
    blob = json.dumps(ev)
    assert token not in blob
    assert token not in caplog.text and SECRET not in caplog.text
    dump = json.dumps(env.agent.store.action_log(500)) + json.dumps(env.agent.store.recent("alice", "s1", 50))
    assert token not in dump and SECRET not in dump
    for r in env.llm.requests:                                   # nor in anything the model saw
        assert token not in json.dumps(r) and "confirmation_token" not in json.dumps(r)


async def test_declined_executes_nothing_and_does_not_reprompt(env):
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    await approve_when_asked(env, approve=False)
    ev = await task
    assert one(ev, "confirmation_declined")["summary"] == "Unlock Front door?"
    assert names(ev).count("confirmation_request") == 1
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert "won't do that" in one(ev, "final")["text"]
    assert [r["status"] for r in env.agent.store.action_log() if r["tool"] == "set_state"][:2] == ["confirmation_declined", "confirmation_requested"]


async def test_no_response_expires(env):
    ctx, ev = await env.chat("unlock the front door", timeout=10)        # ttl is 2 s in the fixture
    exp = one(ev, "confirmation_expired")
    assert exp["reason"] == "no_response" and exp["request_id"].startswith("cr_")
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert "expired" in one(ev, "final")["text"]
    assert names(ev).count("confirmation_request") == 1                  # no automatic re-prompt


async def test_approved_but_token_not_accepted_is_token_expired(env):
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    await approve_when_asked(env, mint_user="mallory")                    # token bound to someone else -> hub: required
    ev = await task
    assert one(ev, "confirmation_expired")["reason"] == "token_expired"
    assert len([c for c in env.fake.calls if c["tool"] == "set_state"]) == 1       # one attempt, not re-issued
    assert names(ev).count("confirmation_request") == 1


async def test_only_the_requesting_user_can_resolve(env):
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    for _ in range(100):
        if env.agent.confirmations.pending_ids():
            break
        await asyncio.sleep(0.02)
    rid = env.agent.confirmations.pending_ids()[0]
    assert env.agent.confirmations.resolve(rid, "bob", False) is False
    assert env.agent.confirmations.resolve(rid, "alice", True, None) is False     # approval without a token is refused
    assert env.agent.confirmations.resolve("cr_nope", "alice", False) is False
    await approve_when_asked(env, approve=False)
    await task


async def test_guest_cannot_trigger_a_confirmation(env):
    ctx, ev = await env.chat("unlock the front door", user="visitor", role="guest", scope=GUEST_SCOPE)
    assert "confirmation_request" not in names(ev)
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert "guests can't" in one(ev, "final")["text"]


async def test_hub_discovered_risk_also_asks(env):
    """Registry cache says normal, the hub says confirmation_required (stale cache): same flow, nothing assumed."""
    d = env.agent.registry.devices["ep_a1000004"]
    from dataclasses import replace
    env.agent.registry.devices["ep_a1000004"] = replace(d, risk="normal")
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    await approve_when_asked(env)
    ev = await task
    assert names(ev).count("confirmation_request") == 1
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 2 and "confirmation_token" not in sets[0]["meta"] and "confirmation_token" in sets[1]["meta"]


async def test_non_user_trigger_only_notifies(env):
    """A rule (or any proactive trigger) on a risk:confirm endpoint: no prompt, no execution, a notification."""
    env.agent.store.add_rule("night lock", "hub.node_status", {"online": False},
                             [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"}}])
    env.agent.bus.publish(__import__("agent.events", fromlist=["Event"]).Event("hub.node_status", {"node_id": "x", "online": False}))
    await env.agent.bus.drain()
    assert env.agent.rules.matched == 1
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert env.agent.confirmations.pending_ids() == []
    n = list(env.agent.notifier.items)
    assert n and n[-1]["level"] == "action_blocked" and "Needs your confirmation" in n[-1]["text"] and "night lock" in n[-1]["text"]
    row = [r for r in env.agent.store.action_log() if r["status"] == "blocked_by_trigger"][0]
    assert row["trigger_kind"] == "rule" and row["trigger_ref"] == "rule:1"


async def test_rule_on_normal_endpoint_runs(env):
    env.agent.store.add_rule("fan", "hub.node_status", {}, [{"tool": "set_state", "args": {
        "endpoint_id": "ep_a1000002", "capability": "power", "value": False}}])
    env.agent.bus.publish(__import__("agent.events", fromlist=["Event"]).Event("hub.node_status", {"node_id": "x"}))
    await env.agent.bus.drain()
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1 and "user_id" not in sets[0]["meta"]            # a rule does not impersonate a user
    r = [r for r in env.agent.store.action_log() if r["tool"] == "set_state"][0]
    assert r["trigger_kind"] == "rule" and r["user_id"] is None


async def test_confirm_endpoint_calls_run_last_and_do_not_block_others(env):
    env.llm.script.append((".", {"calls": [
        {"tool": "set_state", "args": {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "locked"}},
        {"tool": "set_state", "args": {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}}], "say": "ok"}))
    ctx = env.agent.new_request("alice", "s1", "do both")
    task = asyncio.create_task(collect(ctx))
    await approve_when_asked(env, delay=0.3)
    ev = await task
    eps = [c["args"]["endpoint_id"] for c in env.fake.calls if c["tool"] == "set_state"]
    assert eps == ["ep_a1000002", "ep_a1000004"]
    assert names(ev).index("confirmation_request") > [i for i, (n, d) in enumerate(ev) if n == "tool_result"][0]
