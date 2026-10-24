"""(S8o) The browser aborts the chat SSE stream before `final`: the agent cancels the turn. The LLM request is
abandoned (cancelling the await closes the HTTP connection to llama-server, which stops generation), the remaining tool
calls are skipped, calls already executed stay, a pending confirmation keeps its expiry, and the trace says `cancelled`."""
from __future__ import annotations

import asyncio

from conftest import names
from test_confirmation import approve_when_asked


def sets(env):
    return [c for c in env.fake.calls if c["tool"] == "set_state"]


async def closed(ctx, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if ctx.closed:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("turn did not end")


async def test_cancel_while_the_llm_runs_aborts_the_request(env):
    seen = {"started": False, "cancelled": False}
    orig = env.llm.complete

    async def complete(*a, **kw):
        seen["started"] = True
        try:
            return await orig(*a, **kw)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    env.llm.complete = complete
    env.llm.delay = 30.0                                   # would hold the turn for 30 s if not cancelled
    ctx = env.agent.new_request("alice", "s1", "turn on the fan")
    for _ in range(100):
        if seen["started"]:
            break
        await asyncio.sleep(0.02)
    assert ctx.cancel() is True
    await closed(ctx)
    assert seen["cancelled"] and not sets(env)
    events = []
    while not ctx.out.empty():
        item = ctx.out.get_nowait()
        if item:
            events.append(item)
    assert "final" not in names(events)
    tr = env.agent.store.traces(1)[0]
    assert tr["req_id"] == ctx.req_id and tr["detail"]["outcome"] == "cancelled"
    assert env.agent.chat._active == 0                      # the slot is free again


async def test_cancel_skips_the_remaining_calls_but_executed_ones_stay(env):
    env.llm.constrained = False
    env.llm.script.append((r"fan and the lamp", {"calls": [
        {"tool": "set_state", "args": {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}},
        {"tool": "set_state", "args": {"endpoint_id": "ep_a1000006", "capability": "power", "value": True}}], "say": ""}))
    orig = env.agent.executor.run_call
    holder = {}

    async def run_call(ctx, tool, args):
        out = await orig(ctx, tool, args)
        holder["ctx"] = ctx
        ctx.cancel()                                        # the client leaves right after the first call ran
        return out

    env.agent.executor.run_call = run_call
    ctx = env.agent.new_request("alice", "s1", "turn on the fan and the lamp")
    await closed(ctx)
    got = sets(env)
    assert len(got) == 1 and got[0]["args"]["endpoint_id"] == "ep_a1000002"
    tr = env.agent.store.traces(1)[0]["detail"]
    assert tr["outcome"] == "cancelled" and [e["tool"] for e in tr["executed"]] == ["set_state"]


async def test_a_pending_confirmation_survives_the_cancel_until_it_expires(env):
    ctx = env.agent.new_request("alice", "s1", "unlock the front door")
    for _ in range(100):
        if env.agent.confirmations.pending_ids():
            break
        await asyncio.sleep(0.02)
    rid = env.agent.confirmations.pending_ids()[0]
    assert ctx.cancel() is True
    await asyncio.sleep(0.2)
    assert env.agent.confirmations.pending_ids() == [rid] and not sets(env)    # still valid, nothing executed
    assert await approve_when_asked(env) is True
    await closed(ctx)
    assert len(sets(env)) == 1                              # the user's approval still took effect
    assert env.agent.store.traces(1)[0]["detail"]["outcome"] == "cancelled"


async def test_cancel_after_final_is_a_no_op(env):
    ctx, ev = await env.chat("turn on the fan")
    assert "final" in names(ev) and ctx.cancel() is False and not ctx.cancelled


async def test_http_stream_abort_cancels_the_turn(env):
    import httpx
    from agent.app import create_app
    from agent.mock import LocalServer
    env.llm.delay = 30.0
    srv = LocalServer(create_app(env.agent, None))
    url = await srv.start()
    try:
        body = {"user_id": "alice", "session_id": "s1", "text": "turn on the fan", "role": "member"}
        async with httpx.AsyncClient(timeout=10) as c:
            async with c.stream("POST", url + "/v1/chat", json=body) as r:
                assert r.status_code == 200
                async for line in r.aiter_lines():
                    if line.startswith("event: status"):
                        break                               # then leave the `async with`: the connection is closed
        for _ in range(200):
            if env.agent.store.traces(1) and env.agent.store.traces(1)[0]["detail"]["outcome"] == "cancelled":
                break
            await asyncio.sleep(0.05)
        assert env.agent.store.traces(1)[0]["detail"]["outcome"] == "cancelled" and not sets(env)
    finally:
        await srv.stop()
