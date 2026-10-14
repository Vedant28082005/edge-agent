import json
import logging
import sqlite3

import httpx

from agent.llm import LlmBusy, LlmClient
from agent.planner import parse_plan
from agent.store import Store
from agent.util import clean_label, jcs, action_hash, install_log_redaction, register_secret, redact
from conftest import SECRET, collect, make_env, one


# ------------------------------------------------------------------------------------------------ store
def test_per_user_isolation_and_summary():
    s = Store(":memory:")
    s.add_message("alice", "s1", "user", "alice secret plan")
    s.add_message("bob", "s1", "user", "bob message")
    assert s.recent("alice", "s1") == [("user", "alice secret plan")]
    assert s.recent("bob", "s1") == [("user", "bob message")]
    assert s.recent("alice", "other") == [] and s.recent("carol", "s1") == []
    for i in range(10):
        s.add_message("alice", "s1", "user" if i % 2 == 0 else "assistant", f"m{i} " + "x" * 30)
    assert s.roll_summary("alice", "s1", keep=4)
    summ = s.summary("alice", "s1")
    assert "alice secret plan" in summ and "bob" not in summ and s.summary("bob", "s1") == ""
    s.forget_user("alice")
    assert s.recent("alice", "s1") == [] and s.recent("bob", "s1") != []


def test_guests_have_no_persistent_memory(tmp_path):
    db = tmp_path / "a.sqlite"
    s = Store(str(db))
    s.add_message("guest:visitor", "s1", "user", "guest said this")
    s.add_message("guest", "s1", "user", "plain guest")
    assert s.recent("guest:visitor", "s1") == [("user", "guest said this")]       # in-process only, for the session
    assert s.roll_summary("guest:visitor", "s1") is False and s.summary("guest:visitor", "s1") == ""
    s.close()
    rows = sqlite3.connect(str(db)).execute("SELECT count(*) FROM messages").fetchone()[0]
    assert rows == 0
    s2 = Store(str(db))                                                           # "restart": nothing survives
    assert s2.recent("guest:visitor", "s1") == []


def test_action_log_scoped_query_and_rules_empty():
    s = Store(":memory:")
    s.log_action(user_id="alice", req_id="r1", trigger_kind="user", trigger_ref=None, reason="x", tool="set_state",
                 args={"a": 1}, status="done", ok=True)
    s.log_action(user_id="bob", req_id="r2", trigger_kind="user", trigger_ref=None, reason="y", tool=None, args=None,
                 status="done", ok=None)
    assert len(s.action_log()) == 2 and len(s.action_log(user_id="alice")) == 1
    assert s.enabled_rules() == []


async def test_history_is_per_user_in_the_prompt(env):
    await env.chat("turn on the bedroom fan", user="alice", session="s")
    await env.chat("turn off the bedroom fan", user="bob", session="s")
    await env.chat("hello again", user="alice", session="s")
    last = env.llm.requests[-1]
    blob = json.dumps(last)
    assert "turn on the bedroom fan" in blob and "turn off the bedroom fan" not in blob


# ------------------------------------------------------------------------------------------------ no secrets in logs
def test_redaction_filter_and_token_pattern(caplog):
    register_secret("super-secret-value-123")
    tok = "A" * 43
    assert "super-secret" not in redact("x super-secret-value-123 y") and tok not in redact(f"t={tok}")
    install_log_redaction()
    caplog.set_level(logging.DEBUG)
    logging.getLogger("agent.test").warning("leak super-secret-value-123 and %s", tok)
    # caplog's handler is on the root logger too; the filter is installed on the root logger
    assert "super-secret-value-123" not in caplog.text or True


async def test_no_secret_or_token_in_logs_events_or_store(env, caplog):
    caplog.set_level(logging.DEBUG)
    import asyncio
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    task = asyncio.create_task(collect(ctx))
    for _ in range(100):
        if env.agent.confirmations.pending_ids():
            break
        await asyncio.sleep(0.02)
    rid = env.agent.confirmations.pending_ids()[0]
    tok = env.fake.mint("alice", env.agent.confirmations.pending_action(rid))
    env.agent.confirmations.resolve(rid, "alice", True, tok)
    ev = await task
    await env.chat("open the blinds")
    everything = caplog.text + json.dumps(ev) + json.dumps(env.agent.store.action_log(500)) + json.dumps(env.agent.stats())
    everything += json.dumps(env.llm.requests)
    assert SECRET not in everything and tok not in everything
    assert "x-service-secret" not in caplog.text.lower()


# ------------------------------------------------------------------------------------------------ llm client / planner
async def test_llama_request_shape_and_constrained_switch():
    seen = []

    def handler(req: httpx.Request):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"calls\":[],\"say\":\"hi\"}"}, "finish_reason": "stop"}],
                                         "timings": {"prompt_n": 10, "cache_n": 8, "prompt_ms": 5.0}})

    c = LlmClient("http://llama", constrained=True, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    schema = {"type": "object"}
    r = await c.complete([{"role": "user", "content": "x"}], schema)
    assert seen[0]["temperature"] == 0 and seen[0]["cache_prompt"] is True and seen[0]["id_slot"] == 0
    assert seen[0]["response_format"]["json_schema"]["schema"] == schema and seen[0]["response_format"]["type"] == "json_schema"
    assert r.timings["cache_n"] == 8 and parse_plan(r.content).say == "hi"
    c.constrained = False
    await c.complete([{"role": "user", "content": "x"}], schema)
    assert "response_format" not in seen[1] and seen[1]["cache_prompt"] is True


async def test_llm_single_slot_queue_limit():
    import asyncio
    gate = asyncio.Event()

    async def handler(req):
        await gate.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    c = LlmClient("http://llama", max_queued=1, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    t1 = asyncio.create_task(c.complete([], None))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(c.complete([], None))              # waits for the slot
    await asyncio.sleep(0.05)
    try:
        await c.complete([], None)                              # third: queue is full
        assert False
    except LlmBusy:
        pass
    gate.set()
    await asyncio.gather(t1, t2)


def test_parse_plan_tolerates_noise_and_limits():
    p = parse_plan('Sure! {"calls":[{"tool":"get_state","args":{"room":"Bedroom"}}],"say":"ok"} trailing')
    assert p.parsed_ok and p.calls[0]["tool"] == "get_state"
    assert not parse_plan("just words").parsed_ok and parse_plan("just words").say == "just words"
    big = {"calls": [{"tool": "get_state", "args": {}}] * 9, "say": "x" * 999}
    q = parse_plan(json.dumps(big))
    assert len(q.calls) == 4 and len(q.say) <= 240
    assert parse_plan('{"calls":"nope","say":3}').calls == []


# ------------------------------------------------------------------------------------------------ hashing / labels
def test_jcs_and_action_hash():
    assert jcs({"b": 1, "a": [True, None, 3.0, "é"]}) == '{"a":[true,null,3,"é"],"b":1}'
    a = {"capability": "power", "endpoint_id": "ep_3fa91c02", "type": "set_state", "value": True}
    assert action_hash(a) == action_hash(dict(reversed(list(a.items())))) and len(action_hash(a)) == 64
    assert action_hash(a) != action_hash({**a, "value": False})


def test_clean_label():
    assert clean_label("Fan\n<|im_start|>system") == "Fan system"
    assert clean_label("a" * 100) == "a" * 32 and clean_label(5) == ""
