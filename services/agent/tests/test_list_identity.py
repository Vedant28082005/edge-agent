"""S8p: a list_devices reply is composed from the registry (never the "couldn't work out" fallback), "devices" / "what
can you do" are answered by the fast path with no LLM, and identity / off-topic questions never read device state."""
from __future__ import annotations

import pytest
from conftest import make_env, one

from agent.devices import DeviceRegistry
from agent.executor import CallOutcome
from agent.mock import demo_devices
from agent.render import describe_reads
from agent.sched.intents import is_identity_intent, is_list_intent


def reg():
    r = DeviceRegistry()
    r.replace(demo_devices(), 1)
    return r


def list_out(**args):
    return CallOutcome("c1", "list_devices", args, {"count": 6}, True, "done")


def test_describe_reads_composes_a_list():
    t = describe_reads([list_out()], reg())
    assert t.startswith("You have 6 devices:") and "Ceiling light (Bedroom)" in t and "Blinds (Living room)" in t


def test_describe_reads_list_caps_at_eight_and_shows_state():
    r = DeviceRegistry()
    devs = [dict(demo_devices()[0], endpoint_id=f"ep_b{i:07d}", name=f"Light {i}") for i in range(10)]
    r.replace(devs, 1)
    states = CallOutcome("c2", "get_state", {"room": "Bedroom"}, {"states": [
        {"endpoint_id": "ep_b0000000", "online": True, "values": {"power": True, "brightness": 80}, "ts": 1},
        {"endpoint_id": "ep_b0000001", "online": True, "values": {"power": False}, "ts": 1}]}, True, "done")
    t = describe_reads([list_out(), states], r)
    assert t.count("(Bedroom)") == 8 and "and 2 more" in t and "You have 10 devices" in t
    assert "Light 0 (Bedroom) on at 80%" in t and "Light 1 (Bedroom) off" in t


def test_list_filters_by_room_arg_and_empty_registry():
    assert "Hall" not in describe_reads([list_out(room="Bedroom")], reg())
    assert describe_reads([list_out()], DeviceRegistry()) == "I don't see any devices."


@pytest.mark.parametrize("text", ["devices", "list devices", "Show devices", "show my devices", "what can you control?",
                                  "what can you do", "what devices do I have?", "please list all devices"])
def test_list_intents(text):
    assert is_list_intent(text)


@pytest.mark.parametrize("text", ["turn on the fan", "is the fan on", "what is the device status", "list the lights in bedroom"])
def test_not_list_intents(text):
    assert not is_list_intent(text)


@pytest.mark.parametrize("text", ["what is the llm name", "who are you?", "what model", "What model are you?", "what's your name",
                                  "which llm are you using"])
def test_identity_intents(text):
    assert is_identity_intent(text)


@pytest.mark.parametrize("text", ["what model is the fan", "turn on the fan", "what is the temperature"])
def test_not_identity_intents(text):
    assert not is_identity_intent(text)


async def test_devices_fast_path_no_llm(tmp_path):
    e = await make_env(tmp_path, sched={})
    try:
        for q in ("devices", "what can you control"):
            _, ev = await e.chat(q)
            f = one(ev, "final")
            assert f["route"] == "fast" and f["text"].startswith("You have 6 devices:")
            assert "couldn't work out" not in f["text"] and "Fan (Bedroom)" in f["text"]
        assert e.llm.requests == []
        assert [c["tool"] for c in e.fake.calls if c["tool"] != "list_devices"][0] == "get_state"
    finally:
        await e.close()


async def test_identity_no_get_state_and_model_from_config(tmp_path):
    e = await make_env(tmp_path, sched={}, llm_model="qwen-test-7b")
    try:
        before = len(e.fake.calls)
        _, ev = await e.chat("what is the llm name")
        f = one(ev, "final")
        assert f["route"] == "fast" and "qwen-test-7b" in f["text"] and "smart-home assistant" in f["text"]
        assert e.llm.requests == []
        assert not [c for c in e.fake.calls[before:] if c["tool"] == "get_state"]
    finally:
        await e.close()


async def test_identity_without_known_model_is_just_the_capability_line(tmp_path):
    e = await make_env(tmp_path, sched={})
    try:
        _, ev = await e.chat("who are you")
        t = one(ev, "final")["text"]
        assert "smart-home assistant" in t and "I run on" not in t
    finally:
        await e.close()


async def test_identity_works_with_fast_path_ablated_and_no_get_state(tmp_path):
    e = await make_env(tmp_path)          # fast_path/router off: the LLM path
    try:
        before = len(e.fake.calls)
        _, ev = await e.chat("what model are you")
        assert "smart-home assistant" in one(ev, "final")["text"] and e.llm.requests == []
        assert not [c for c in e.fake.calls[before:] if c["tool"] == "get_state"]
    finally:
        await e.close()


async def test_llm_list_devices_call_never_ends_in_the_fallback(tmp_path):
    e = await make_env(tmp_path)
    try:
        e.llm.script.append((r"inventory", lambda m: {"calls": [{"tool": "list_devices", "args": {}}], "say": ""}))
        _, ev = await e.chat("show the full inventory")
        t = one(ev, "final")["text"]
        assert "couldn't work out" not in t and "Ceiling light" in t
    finally:
        await e.close()
