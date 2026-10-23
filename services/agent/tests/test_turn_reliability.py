"""(S8g) Reliability of the chat turn under a slow or unhelpful model.

1. Keepalive: a slow LLM round sends periodic `status` events, so a long-idle SSE stream (the actual bug: a gateway
   or proxy read-timeout gave up on a 40-65s turn before `final` arrived) never goes quiet for long.
2. Safe fallbacks: no tool call and no text names a real device instead of silently saying "OK"; a plan that fails
   validation is retried once with the reason, then falls back honestly if that doesn't fix it either.
3. Every turn still ends with exactly one `final` event, built from verified results."""
from __future__ import annotations

import asyncio

from conftest import names, one


async def test_slow_llm_round_sends_keepalive_status_events(env):
    env.llm.delay = 0.35
    import agent.chat as chat_mod
    orig = chat_mod.KEEPALIVE_S
    chat_mod.KEEPALIVE_S = 0.1
    try:
        ctx, ev = await env.chat("hello there")
    finally:
        chat_mod.KEEPALIVE_S = orig
    statuses = [d for n, d in ev if n == "status"]
    assert len(statuses) >= 2                       # the immediate one, plus at least one keepalive ping
    assert names(ev)[-1] == "final" or names(ev).count("final") == 1
    one(ev, "final")                                 # the turn still ends normally


async def test_no_tool_call_and_no_text_names_a_real_device(env):
    env.llm.script.append((r"^xyzzy$", {"calls": [], "say": ""}))
    ctx, ev = await env.chat("xyzzy")
    final = one(ev, "final")["text"]
    assert final.startswith("I couldn't work out what to do.")
    assert "turn on" in final                         # names a real device from the registry, never invented


async def test_plan_that_fails_validation_is_retried_then_recovers(env):
    env.llm.constrained = False          # an invalid endpoint id must pass the (simulated) grammar to reach validate_call
    calls = {"n": 0}

    def reply(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_deadbeef", "capability": "power", "value": True}}],
                    "say": "Turning it on."}
        return {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}}],
                "say": "Turning on the fan."}

    env.llm.script.append((r"turn on the mystery fan", reply))
    ctx, ev = await env.chat("turn on the mystery fan")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1 and sets[0]["args"]["endpoint_id"] == "ep_a1000002"
    assert calls["n"] == 2                            # one retry, not an unbounded loop


async def test_plan_that_keeps_failing_validation_falls_back_honestly(env):
    env.llm.constrained = False
    env.llm.script.append((r"turn on the ghost fan",
                           {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_deadbeef", "capability": "power", "value": True}}],
                            "say": "Turning it on."}))
    ctx, ev = await env.chat("turn on the ghost fan")
    final = one(ev, "final")["text"]
    assert "couldn't" in final.lower() or "doesn" in final.lower()
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]    # nothing reached the hub, never claimed
