"""S8e regressions: a power-on turn never leaves a dimmable light at brightness 0, and a text-only reply to an
imperative request is never trusted at face value when no tool call ran."""
from conftest import names, one

from agent.prompt import SYSTEM_PROMPT


def test_system_prompt_states_the_brightness_floor():
    assert "never lowers brightness below 10" in SYSTEM_PROMPT


async def test_power_on_plus_brightness_zero_is_repaired_to_the_floor(env):
    env.llm.script.append((r"please light up the ceiling", {
        "calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}},
                  {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}],
        "say": "Turning on the ceiling light."}))
    ctx, ev = await env.chat("please light up the ceiling")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 2
    bright = next(c for c in sets if c["args"]["capability"] == "brightness")
    assert bright["args"]["value"] == 10                     # never 0, never applied silently
    power = next(c for c in sets if c["args"]["capability"] == "power")
    assert power["args"]["value"] is True


async def test_lone_brightness_zero_on_turn_on_is_repaired(env):
    env.llm.script.append((r"turn on the lone ceiling light", {
        "calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}],
        "say": "Turning on the ceiling light."}))
    ctx, ev = await env.chat("turn on the lone ceiling light")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1 and sets[0]["args"]["value"] == 10


async def test_explicit_zero_level_from_the_user_is_honoured(env):
    env.llm.script.append((r"ceiling light to 0 percent", {
        "calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "power", "value": True}},
                  {"tool": "set_state", "args": {"endpoint_id": "ep_a1000001", "capability": "brightness", "value": 0}}],
        "say": "Setting the ceiling light."}))
    ctx, ev = await env.chat("set the ceiling light to 0 percent")
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    bright = next(c for c in sets if c["args"]["capability"] == "brightness")
    assert bright["args"]["value"] == 0


async def test_text_only_claim_on_an_imperative_request_is_retried_then_flagged(env):
    env.llm.script.append((r"turn on the mystery lamp", {"calls": [], "say": "Turning it on."}))
    ctx, ev = await env.chat("turn on the mystery lamp")
    final = one(ev, "final")
    assert final["text"] == "I didn't make that change — could you say it again?"
    assert not [c for c in env.fake.calls if c["tool"] == "set_state"]
    # it was given a second chance before being flagged
    assert sum(1 for m in env.llm.requests if "mystery lamp" in m[-1]["content"]) == 2
    assert env.agent.metrics.claims_without_calls == 1


async def test_text_only_claim_that_fixes_itself_on_retry_is_not_flagged(env):
    calls = {"n": 0}

    def reply(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"calls": [], "say": "Turning it on."}
        return {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}}],
                "say": "Turning on the fan."}

    env.llm.script.append((r"flaky fan", reply))
    ctx, ev = await env.chat("turn on the flaky fan")
    final = one(ev, "final")
    assert "I didn't make that change" not in final["text"]
    assert env.agent.metrics.claims_without_calls == 0
    assert calls["n"] == 2


async def test_clarifying_question_without_calls_is_left_alone(env):
    ctx, ev = await env.chat("do the thing")
    final = one(ev, "final")
    assert final["text"] == "I'm not sure which device you mean."
    assert env.agent.metrics.claims_without_calls == 0
