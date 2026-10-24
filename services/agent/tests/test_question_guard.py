"""(S8j) Bug: "what is the device status?" produced a `get_state` call AND a stray `set_state` that turned a light
off. A question or status request must never cause a device write, whatever the model (or a fast-path bug) proposes.

Covers: the general intent gate on the LLM path (writes stripped, logged, read-only reply composed), the mixed
request that still acts ("is it on? turn it off"), the no-LLM fast path for simple status questions (one named
device, or all devices), the ambient fast path (acts on one candidate, asks with several), and the ambient fallback
used instead of the generic "I couldn't work out what to do" message when the LLM itself gives up on an ambient
complaint."""
from __future__ import annotations

from conftest import collect, make_env, names, one
from test_ambient_polarity import ONE_LIGHT, custom_env

from agent.mock import demo_devices


async def fast_env(tmp_path, **sched):
    sched.setdefault("router", False)
    return await make_env(tmp_path, sched=sched)


def hub_sets(e):
    return [c for c in e.fake.calls if c["tool"] == "set_state"]


# --------------------------------------------------------------------------------------------- LLM-path intent gate
async def test_question_never_writes_even_when_the_model_proposes_it(env):
    """Reproduces the bug: the (buggy) model's plan for a status question includes a stray set_state. The gate
    strips it before it ever reaches the executor/hub."""
    def reply(_messages, state={"n": 0}):
        state["n"] += 1
        if state["n"] > 1:
            return {"calls": [], "say": "Here is what I found."}
        return {"calls": [{"tool": "get_state", "args": {"endpoint_id": "ep_a1000001"}},
                          {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": False}}],
                "say": "Checking."}

    env.llm.script.append((r"device status", reply))
    ctx, ev = await env.chat("what is the device status?")
    assert not hub_sets(env)                                       # the write never reached the hub
    final = one(ev, "final")
    assert "Ceiling light" in final["text"]
    assert env.agent.metrics.writes_stripped_for_question == 1


async def test_mixed_question_and_command_still_acts(env):
    """"is it on? turn it off" has an imperative verb too: the gate must never block it."""
    def reply(_messages, state={"n": 0}):
        state["n"] += 1
        if state["n"] > 1:
            return {"calls": [], "say": "Done."}
        return {"calls": [{"tool": "get_state", "args": {"endpoint_id": "ep_a1000001"}},
                          {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": False}}],
                "say": "Turning it off."}

    env.llm.script.append((r"turn it off", reply))
    ctx, ev = await env.chat("is it on? turn it off")
    sets = hub_sets(env)
    assert sets and sets[0]["args"] == {"endpoint_id": "ep_a1000001", "capability": "power", "value": False}
    assert env.agent.metrics.writes_stripped_for_question == 0


async def test_plain_question_phrasings_produce_no_writes(env):
    for text in ("is the ceiling light on?", "what is the ceiling light status", "how is the fan"):
        env.llm.script.append((r".", {
            "calls": [{"tool": "get_state", "args": {"endpoint_id": "ep_a1000001"}},
                      {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}}],
            "say": "Checking."}))
        ctx, ev = await env.chat(text)
        assert not hub_sets(env), text
        env.llm.script.clear()


# --------------------------------------------------------------------------------------------- fast-path status questions
async def test_fast_path_answers_a_single_device_status_question_without_the_llm(tmp_path):
    e = await custom_env(tmp_path, ONE_LIGHT, fast_path=True, router=False)
    try:
        e.fake.state["ep_a1000001"]["power"] = True
        e.fake.state["ep_a1000001"]["brightness"] = 42
        ctx, ev = await e.chat("what is the ceiling light status?")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        assert "Ceiling light" in final["text"] and "42" in final["text"]
        assert not hub_sets(e)
    finally:
        await e.close()


async def test_fast_path_answers_an_unqualified_status_question_with_every_device(tmp_path):
    e = await fast_env(tmp_path, fast_path=True)
    try:
        ctx, ev = await e.chat("what is the device status?")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        for d in demo_devices():
            assert d["name"] in final["text"]
        assert not hub_sets(e)
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- ambient fast path
async def test_ambient_fast_path_acts_without_the_llm_with_one_candidate(tmp_path):
    e = await custom_env(tmp_path, ONE_LIGHT, fast_path=True, router=False)
    try:
        ctx, ev = await e.chat("it's dark in here")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        sets = hub_sets(e)
        assert sets and sets[0]["args"]["value"] is True
    finally:
        await e.close()


async def test_ambient_fast_path_asks_with_several_candidates(tmp_path):
    """The demo registry has two lights: "it's dark" (no device named) asks instead of guessing, without the LLM."""
    e = await fast_env(tmp_path, fast_path=True)
    try:
        ctx, ev = await e.chat("it's dark in here")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        assert not hub_sets(e)
        assert "?" in final["text"]
        assert e.agent.metrics.ambient_clarifications == 1
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- ambient empty-plan fallback
async def test_empty_llm_plan_for_an_ambient_complaint_acts_instead_of_the_generic_message(tmp_path):
    """fast_path disabled: when the model gives up (empty plan) on an ambient complaint, the ambient rule -- not
    the generic "I couldn't work out what to do" text -- decides, acting on the one candidate device."""
    e = await custom_env(tmp_path, ONE_LIGHT)
    try:
        e.llm.script.append((r"dark in here", {"calls": [], "say": "not sure"}))
        ctx, ev = await e.chat("it's dark in here")
        final = one(ev, "final")
        assert "couldn't work out" not in final["text"]
        sets = hub_sets(e)
        assert sets and sets[0]["args"]["value"] is True
    finally:
        await e.close()


async def test_empty_llm_plan_for_an_ambient_complaint_asks_with_several_candidates(env):
    env.llm.script.append((r"dark in here", {"calls": [], "say": "not sure"}))
    ctx, ev = await env.chat("it's dark in here")
    final = one(ev, "final")
    assert not hub_sets(env)
    assert "?" in final["text"]
    assert env.agent.metrics.ambient_clarifications == 1
