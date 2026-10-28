"""Model-independent `say` validation, using malformed outputs captured from a real command-tuned fine-tune."""
from __future__ import annotations

import pytest
from conftest import make_env, one

from agent.aliases import AliasMap
from agent.mock import demo_devices
from agent.planner import parse_plan
from agent.devices import DeviceRegistry
from agent.reply import CAPABILITY_TEXT, is_conversation_turn, malformed_say, say_verdict

CUT_OFF = "I'm your smart-home assistant: I can control your lights, fans, locks and other"


def _reg() -> DeviceRegistry:
    r = DeviceRegistry()
    r.replace(demo_devices(), 1)
    return r


@pytest.mark.parametrize("say", ["e1", "g", "s", "power", "]On it.", "On it", "Hi there", "", "...", "12 34 56.",
                                 'He said "hello there".', "Okay {done} now.", "Sure (really.", "It's 'fine now.",
                                 "I'm here to help", CUT_OFF, "I can help� you now."])
def test_malformed_says_are_rejected(say):
    assert malformed_say(say, _reg())


@pytest.mark.parametrize("say", ["I'm here to help!", "The lamp is on.", "Sure, I can do that for you.", CAPABILITY_TEXT])
def test_clean_says_pass(say):
    assert malformed_say(say, _reg()) is None


def test_device_name_is_not_a_say():
    reg = _reg()
    name = next(iter(reg.devices.values())).name
    assert malformed_say(name, reg) and malformed_say(name + ".", reg)


def test_say_verdict_rejects_action_claims_without_a_call():
    reg = _reg()
    for s in ("On it, right away.", "Done, all finished.", "Turning on the lights."):
        assert say_verdict(s, [], reg, "", "hello") is not None
    assert say_verdict("I'm here to help!", [], reg, "", "hello") is None


def test_parse_plan_fixtures_give_no_calls_and_the_raw_junk_say():
    a = AliasMap()
    assert parse_plan('["s","e1","power","]On it."]', a).calls == []
    p = parse_plan('[["g","e1"],"I\'m here to help!"]', a)
    assert p.say == "I'm here to help!"


def test_conversation_turn_detection():
    reg = _reg()
    reads = [{"tool": "get_state", "args": {"endpoint_id": next(iter(reg.devices))}}]
    for t in ("hello", "thanks!", "how are you", "who are you"):
        assert is_conversation_turn(t, reg, reads)
    assert not is_conversation_turn("is the lamp on?", reg, reads)
    assert not is_conversation_turn("what's the status", reg, reads)
    assert not is_conversation_turn("hello", reg, [{"tool": "set_state", "args": {}}])
    assert not is_conversation_turn("hello", reg, [])


async def _turn(tmp_path, text, script, **sched):
    e = await make_env(tmp_path, sched={"route_mode": "llm_first", "router": False, **sched})
    e.llm.emit_say = True
    e.llm.script.append((r".", script))
    return e, (await e.chat(text))[1]


def reads(e):
    return [c for c in e.fake.calls if c["tool"] in ("get_state", "read_sensor")]


@pytest.mark.parametrize("say_in_plan", [False, True])
async def test_greeting_with_a_read_runs_no_read(tmp_path, say_in_plan):
    e, ev = await _turn(tmp_path, "hello", {"calls": [{"tool": "get_state", "args": {"endpoint_id": "ep_a1000006"}}],
                                            "say": "I'm here to help!"}, say_in_plan=say_in_plan)
    try:
        assert not reads(e)
        txt = one(ev, "final")["text"]
        assert txt in (CAPABILITY_TEXT, "I'm here to help!")
    finally:
        await e.close()


@pytest.mark.parametrize("junk", ["e1", "g", "]On it.", CUT_OFF])
async def test_junk_say_with_no_calls_gets_the_capability_line(tmp_path, junk):
    e, ev = await _turn(tmp_path, "thanks", {"calls": [], "say": junk}, say_in_plan=True)
    try:
        txt = one(ev, "final")["text"]
        assert txt == CAPABILITY_TEXT and junk not in txt
    finally:
        await e.close()
