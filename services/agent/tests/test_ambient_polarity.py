"""(S8h) A vague ambient complaint ("it's dark/bright/hot/cold/stuffy in here") names no device and no number.

Bug: "it's bright here" produced the same plan (light on at 70%) as "it's dark in here" -- a plan whose direction
contradicts the complaint. `ambient_request_signal` / `plan_domain_direction` (schema.py) detect the mismatch; the
chat turn loop (chat.py) retries once with the contradiction stated, then falls back to a clarifying question that
names real devices. The same clarifying preference applies whenever more than one device could answer the
complaint, even when the model's direction was fine -- acting silently on one of several candidates is never
safe for a request that named none."""
from __future__ import annotations

from conftest import SECRET, Env, names, one

from agent.config import AgentConfig
from agent.mock import FakeHub, LocalServer, ScriptedLLM, demo_devices
from agent.service import Agent

ONE_LIGHT = [demo_devices()[0]]        # just "Ceiling light" (power + brightness), no second light to disambiguate


async def custom_env(tmp_path, devices, **sched):
    """Like conftest's `make_env`, but with a chosen device set; the LLM path is exercised (fast path off unless
    the caller asks for it, same default as `env`)."""
    secret_file = tmp_path / "agent.secret"
    secret_file.write_text(SECRET)
    fake = FakeHub(SECRET, devices=devices, settle_delay=0.05)
    server = LocalServer(fake.app())
    url = await server.start()
    sched.setdefault("fast_path", False)
    sched.setdefault("router", False)
    cfg = AgentConfig(hub_url=url, hub_secret_file=str(secret_file), db_path=":memory:", confirmation_ttl_s=2.0,
                      settle_wait_s=2.0, sched=sched)
    llm = ScriptedLLM(cfg.constrained)
    agent = Agent.from_config(cfg, llm)
    await agent.start()
    return Env(agent, fake, llm, server, tmp_path)


def _bright_plan(value: bool) -> dict:
    return {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": value}}],
            "say": f"Turning the ceiling light {'on' if value else 'off'}."}


async def test_dark_request_turns_the_only_light_on(tmp_path):
    """The correct direction, exactly one candidate device: acts without asking."""
    e = await custom_env(tmp_path, ONE_LIGHT)
    try:
        e.llm.script.append((r"dark in here", _bright_plan(True)))
        ctx, ev = await e.chat("it's dark in here")
        sets = [c for c in e.fake.calls if c["tool"] == "set_state"]
        assert sets and sets[0]["args"]["value"] is True
        assert e.agent.metrics.ambient_clarifications == 0
    finally:
        await e.close()


async def test_bright_request_contradiction_is_retried_then_fixed(tmp_path):
    """"it's bright here" must never turn a light ON: the first (wrong) plan is retried once with the
    contradiction stated, and a model that corrects itself on the retry is never flagged."""
    e = await custom_env(tmp_path, ONE_LIGHT)
    try:
        calls = {"n": 0}

        def reply(_messages):
            calls["n"] += 1
            return _bright_plan(True) if calls["n"] == 1 else _bright_plan(False)

        e.llm.script.append((r"bright here", reply))
        ctx, ev = await e.chat("it's too bright here")
        sets = [c for c in e.fake.calls if c["tool"] == "set_state"]
        assert len(sets) == 1 and sets[0]["args"]["value"] is False       # the corrected (off) plan, not the first
        assert calls["n"] == 2
        assert e.agent.metrics.ambient_clarifications == 0
    finally:
        await e.close()


async def test_bright_request_that_never_corrects_becomes_a_clarifying_question(tmp_path):
    """A model that keeps contradicting the complaint after one retry is never trusted to act at all."""
    e = await custom_env(tmp_path, ONE_LIGHT)
    try:
        e.llm.script.append((r"bright here", _bright_plan(True)))      # always wrong: turns the light ON
        ctx, ev = await e.chat("it's way too bright here")
        final = one(ev, "final")
        assert not [c for c in e.fake.calls if c["tool"] == "set_state"]      # nothing was actually changed
        assert "Ceiling light" in final["text"] and "?" in final["text"]
        in_request = [any("too bright here" in m["content"] for m in req) for req in e.llm.requests]
        assert sum(in_request) == 2     # the original plan, then one retry -- no more
        assert e.agent.metrics.ambient_clarifications == 1
    finally:
        await e.close()


async def test_ambient_request_with_more_than_one_candidate_device_asks_even_when_direction_is_right(env):
    """The demo registry has two lights (Ceiling light, Lamp): a vague "it's dark" naming neither must ask which
    one, even though the model's own direction (turning a light on) was correct."""
    env.llm.script.append((r"dark in here", {
        "calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}}],
        "say": "Turning on the ceiling light."}))
    ctx, ev = await env.chat("it's dark in here")
    final = one(ev, "final")
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert "?" in final["text"]
    assert env.agent.metrics.ambient_clarifications == 1


async def test_cold_request_maps_to_heating_not_cooling(tmp_path):
    """temp domain: "freezing" wants heat; a plan that sets mode=cool contradicts it."""
    thermostat = [{"endpoint_id": "ep_b0000001", "name": "Thermostat", "room": "Hall", "type": "thermostat",
                   "tags": [], "online": True, "risk": "normal",
                   "capabilities": [{"id": "power", "kind": "actuator", "type": "bool"},
                                     {"id": "mode", "kind": "actuator", "type": "enum", "values": ["cool", "heat", "off"]}]}]
    e = await custom_env(tmp_path, thermostat)
    try:
        def cool_then_heat(messages, state={"n": 0}):
            state["n"] += 1
            mode = "cool" if state["n"] == 1 else "heat"
            return {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_b0000001", "capability": "mode", "value": mode}}],
                    "say": f"Switching to {mode}."}

        e.llm.script.append((r"freezing", cool_then_heat))
        ctx, ev = await e.chat("it's freezing in here")
        sets = [c for c in e.fake.calls if c["tool"] == "set_state" and c["args"]["capability"] == "mode"]
        assert sets and sets[-1]["args"]["value"] == "heat"
    finally:
        await e.close()


async def test_explicit_device_named_skips_the_ambient_guard(env):
    """A request that names a real device by name is never treated as an ambient (device-less) complaint, even
    if it also uses one of the ambient words -- the ordinary plan runs untouched."""
    env.llm.script.append((r"ceiling light.*bright", {
        "calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}}],
        "say": "Turning on the ceiling light."}))
    ctx, ev = await env.chat("the ceiling light is too bright, turn it on anyway")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert sets and sets[0]["args"]["value"] is True
    assert env.agent.metrics.ambient_clarifications == 0


async def test_fast_path_relative_reply_states_the_new_value(tmp_path):
    """(S8h) The fast-path "brighter/dimmer" reply must say the new value, not a bare "Done.""."""
    e = await custom_env(tmp_path, [demo_devices()[0]], fast_path=True, router=False)
    try:
        e.fake.state["ep_a1000001"]["brightness"] = 50
        ctx, ev = await e.chat("make the ceiling light brighter")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        assert final["text"] == "Ceiling light raised to 60%."
    finally:
        await e.close()
