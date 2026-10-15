import asyncio
import json

from conftest import GUEST_SCOPE, make_env, names, one
from agent.llm import LlmResult


def script(env, calls, say="ok"):
    env.llm.script.append((".", {"calls": calls, "say": say}))


def setstate(ep, cap, value, **extra):
    return {"tool": "set_state", "args": {"endpoint_id": ep, "capability": cap, "value": value, **extra}}


async def test_invalid_endpoint_rejected_by_executor_in_unconstrained_mode(env):
    env.agent.llm.constrained = False                       # the S10 ablation: no grammar, the executor must still protect
    ctx, ev = await env.chat("turn on the unicorn [hallucinate]")
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]          # nothing reached the hub
    tr = one(ev, "tool_result")
    assert tr["ok"] is False and tr["result"]["error"] == "unknown_endpoint" and tr["result"]["rejected_by"] == "agent"
    assert env.agent.metrics.calls_rejected == {"unknown_endpoint": 1} and env.agent.metrics.calls_executed == 0
    assert "couldn't" in one(ev, "final")["text"].lower() or "doesn" in one(ev, "final")["text"].lower()


async def test_constrained_mode_makes_the_same_request_impossible(env):
    assert env.agent.llm.constrained is True
    ctx, ev = await env.chat("turn on the unicorn [hallucinate]")
    assert "tool_call" not in names(ev)                      # the (simulated) grammar produced no call at all
    assert not env.fake.calls or all(c["tool"] != "set_state" for c in env.fake.calls)
    assert env.agent.metrics.calls_rejected == {}


async def test_runtime_switch_changes_the_request_to_llama(env):
    seen = []
    orig = env.agent.llm.complete

    async def spy(messages, schema):
        seen.append(schema is not None)
        return await orig(messages, schema)

    env.agent.llm.complete = spy
    await env.chat("turn on the bedroom fan")
    env.agent.llm.constrained = False
    await env.chat("turn off the bedroom fan")
    env.agent.llm.constrained = True
    await env.chat("turn on the bedroom fan")
    assert seen == [True, False, True]


async def test_model_supplied_token_and_user_id_are_stripped(env):
    env.agent.llm.constrained = False          # in constrained mode the grammar would not even allow these keys
    script(env, [setstate("ep_a1000002", "power", True, confirmation_token="STOLEN", user_id="admin", call_id="x", _meta={"a": 1})])
    ctx, ev = await env.chat("hello")
    sent = [c for c in env.fake.calls if c["tool"] == "set_state"][0]
    assert set(sent["args"]) == {"endpoint_id", "capability", "value"}
    assert sent["meta"].get("user_id") == "alice" and "confirmation_token" not in sent["meta"]
    assert sent["meta"]["call_id"] != "x"
    assert "STOLEN" not in json.dumps(ev) and env.agent.metrics.stripped_fields == 4


async def test_budget_max_calls(tmp_path):
    e = await make_env(tmp_path, max_calls=2)
    try:
        script(e, [setstate("ep_a1000002", "power", True), setstate("ep_a1000001", "power", True),
                   setstate("ep_a1000006", "power", True)])
        ctx, ev = await e.chat("hello")
        sets = [c for c in e.fake.calls if c["tool"] == "set_state"]
        assert len(sets) == 2
        results = [d for n, d in ev if n == "tool_result"]
        assert results[-1]["result"].get("error") is None or True
        assert "ran out of budget" in one(ev, "final")["text"]
    finally:
        await e.close()


async def test_budget_max_steps_stops_a_read_loop(tmp_path):
    e = await make_env(tmp_path, max_steps=2)
    try:
        n = []

        def always_read(messages):
            n.append(1)
            return {"calls": [{"tool": "get_state", "args": {"room": "Bedroom"}}], "say": "again"}

        e.llm.script.append((".", always_read))
        ctx, ev = await e.chat("what is going on")
        assert len(n) == 2                                    # exactly max_steps planner rounds
        assert names(ev)[-1] == "final" and "limit" in one(ev, "final")["text"]
    finally:
        await e.close()


async def test_budget_zero_steps_is_an_error_event(tmp_path):
    e = await make_env(tmp_path, max_steps=0)
    try:
        ctx, ev = await e.chat("turn on the fan")
        assert one(ev, "error")["code"] == "budget_exceeded" and names(ev)[-1] == "error"
    finally:
        await e.close()


async def test_deadline_budget(tmp_path):
    e = await make_env(tmp_path, deadline_s=0.05)
    try:
        e.llm.delay = 0.2
        ctx, ev = await e.chat("what is the temperature in the bedroom")      # the planner round used up the deadline
        assert names(ev)[-1] == "final" and "ran out of budget" in one(ev, "final")["text"]
        assert not [c for c in e.fake.calls if c["tool"] == "read_sensor"]
    finally:
        await e.close()


async def test_retry_on_ack_timeout_then_success(env):
    env.fake.fail_next = ["ack_timeout"]
    ctx, ev = await env.chat("turn on the bedroom fan")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 2 and sets[0]["args"] == sets[1]["args"]
    assert one(ev, "tool_result")["ok"] is True
    assert ctx.budget.retries == 1


async def test_retry_budget_exhausted(env):
    env.fake.fail_next = ["ack_timeout", "ack_timeout", "ack_timeout"]
    ctx, ev = await env.chat("turn on the bedroom fan")
    assert len([c for c in env.fake.calls if c["tool"] == "set_state"]) == 2        # 1 try + max_retries(1)
    assert one(ev, "tool_result")["ok"] is False
    assert "did not respond" in one(ev, "final")["text"]


async def test_no_retry_on_device_error_or_offline(env):
    env.fake.fail_next = ["device_error"]
    ctx, ev = await env.chat("turn on the bedroom fan")
    assert len([c for c in env.fake.calls if c["tool"] == "set_state"]) == 1
    env.fake.fail_next = ["offline"]
    ctx, ev = await env.chat("turn off the bedroom fan")
    assert len([c for c in env.fake.calls if c["tool"] == "set_state"]) == 2
    assert "offline" in one(ev, "final")["text"]


async def test_unverified_result_is_reported_not_retried(env):
    orig = env.fake.tool

    def tool(name, a, meta):
        r = orig(name, a, meta)
        if name == "set_state":
            r = {**r, "verified": False}
        return r

    env.fake.tool = tool
    ctx, ev = await env.chat("turn on the bedroom fan")
    assert len([c for c in env.fake.calls if c["tool"] == "set_state"]) == 1
    assert "couldn't confirm" in one(ev, "final")["text"]


async def test_action_log_has_trigger_reason_action_result(env):
    ctx, ev = await env.chat("turn on the bedroom fan", user="alice")
    rows = [r for r in env.agent.store.action_log() if r["tool"] == "set_state"]
    assert len(rows) == 1
    r = rows[0]
    assert r["trigger_kind"] == "user" and r["user_id"] == "alice" and r["req_id"] == ctx.req_id
    assert r["reason"].startswith("user request:") and "bedroom fan" in r["reason"]
    assert json.loads(r["args"]) == {"capability": "power", "endpoint_id": "ep_a1000002", "value": True}
    assert r["status"] == "done" and r["ok"] == 1
    assert json.loads(r["detail"])["result"]["verified"] is True


async def test_failed_call_is_logged(env):
    env.fake.fail_next = ["device_error"]
    await env.chat("turn on the bedroom fan")
    r = [r for r in env.agent.store.action_log() if r["tool"] == "set_state"][0]
    assert r["status"] == "failed" and r["ok"] == 0


async def test_only_five_tools_can_be_called(env):
    import pytest
    with pytest.raises(ValueError):
        await env.agent.hub.call_tool("delete_endpoint", {})
    from agent.hub_client import TOOLS
    assert sorted(TOOLS) == ["get_state", "list_devices", "read_sensor", "run_scene", "set_state"]


async def test_read_loop_feeds_results_back_and_answers(env):
    ctx, ev = await env.chat("what is the temperature in the bedroom")
    assert [c["tool"] for c in env.fake.calls if c["tool"] != "list_devices"] == ["read_sensor"]
    assert "24.5" in one(ev, "final")["text"]
    assert len(env.llm.requests) == 2
    second = env.llm.requests[1]
    assert second[0] == env.llm.requests[0][0]                   # prefix message identical across rounds
    assert second[-1]["content"].startswith("Tool results:") and "temperature=24.5" in second[-1]["content"]


async def test_llm_unavailable_and_busy_are_error_events(env):
    from agent.llm import LlmBusy, LlmError

    async def busy(m, s):
        raise LlmBusy("x")

    env.agent.llm.complete = busy
    ctx, ev = await env.chat("turn on the fan")
    assert one(ev, "error")["code"] == "llm_busy"

    async def down(m, s):
        raise LlmError("x")

    env.agent.llm.complete = down
    ctx, ev = await env.chat("turn on the fan")
    assert one(ev, "error")["code"] == "llm_busy"


async def test_too_many_concurrent_turns_get_llm_busy(tmp_path):
    e = await make_env(tmp_path, max_queued_turns=1)
    try:
        e.llm.delay = 0.3
        ctxs = [e.agent.new_request("alice", f"s{i}", "turn on the bedroom fan") for i in range(4)]
        from conftest import collect
        res = await asyncio.gather(*[collect(c) for c in ctxs])
        codes = [d.get("code") for ev in res for n, d in ev if n == "error"]
        assert codes.count("llm_busy") == 2
    finally:
        await e.close()


async def test_guest_llm_proposed_set_state_and_run_scene_are_denied_and_logged(env):
    """Fail closed on the executor itself: even a (hallucinated or injected) LLM plan by a guest cannot move anything,
    on a normal-risk device, with a clear denial result and one action-log row per call."""
    script(env, [setstate("ep_a1000002", "power", True), {"tool": "run_scene", "args": {"scene": "Good night"}}])
    ctx, ev = await env.chat("turn on the bedroom fan", user="visitor", role="guest", scope=GUEST_SCOPE)
    assert not [c for c in env.fake.calls if c["tool"] in ("set_state", "run_scene")]       # nothing reached the hub
    res = [d for n, d in ev if n == "tool_result"]
    assert len(res) == 2 and all(r["ok"] is False and r["result"]["error"] == "not_allowed"
                                 and r["result"]["reason"] == "guest_role" for r in res)
    assert "confirmation_request" not in names(ev) and not env.agent.confirmations.pending_ids()
    rows = [r for r in env.agent.store.action_log(50) if r["status"] == "denied_guest"]
    assert sorted(r["tool"] for r in rows) == ["run_scene", "set_state"] and all(r["ok"] == 0 for r in rows)
    assert env.agent.metrics.calls_executed == 0


async def test_guest_run_approved_is_denied_too(env):
    action = {"type": "set_state", "endpoint_id": "ep_a1000002", "capability": "power", "value": True}
    ctx = env.agent.new_request("visitor", "s1", "retry", "guest")
    out = await env.agent.executor.run_approved(ctx, action, "tok")
    assert out.state == "denied" and not [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert [r["status"] for r in env.agent.store.action_log(5)][0] == "denied_guest"
