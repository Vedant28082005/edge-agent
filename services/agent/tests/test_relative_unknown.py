"""(S8n) Relative changes in any common wording run on the fast path (no LLM), and a device the user names that is
not in the registry is answered with the real device names instead of a guess."""
from __future__ import annotations

import pytest
from conftest import one
from test_sched_resolver_generic import custom_env

from agent.devices import DeviceRegistry
from agent.sched.resolver import FALLTHROUGH, RELATIVE_PENDING, FastPathResolver

LIGHT = [{"endpoint_id": "ep_d0000001", "name": "Test light", "room": "Lab", "type": "light", "tags": [],
          "online": True, "risk": "normal",
          "capabilities": [{"id": "power", "kind": "actuator", "type": "bool"},
                           {"id": "brightness", "kind": "actuator", "type": "int", "min": 0, "max": 100}]}]
EP = "ep_d0000001"


def reg():
    r = DeviceRegistry()
    r.replace(LIGHT, 1)
    return r


DOWN = ["decrease the test light by 10", "reduce the test light by 10 percent", "lower the test light by 10%",
        "dim the test light by 10", "drop the test light by 10", "turn down the test light by 10",
        "make the test light a bit dimmer", "test light a little lower", "dim the test light a bit"]
UP = ["increase the test light by 10", "raise the test light by 10 percent", "boost the test light by 10",
      "turn up the test light by 10", "make the test light a bit brighter", "test light a little higher"]


@pytest.mark.parametrize("text", DOWN)
def test_resolver_relative_down(text):
    res = FastPathResolver().resolve(text, reg(), "member", ())
    assert res.status == RELATIVE_PENDING, res.reason
    assert res.relative[:3] == (EP, "brightness", -1)


@pytest.mark.parametrize("text", UP)
def test_resolver_relative_up(text):
    res = FastPathResolver().resolve(text, reg(), "member", ())
    assert res.status == RELATIVE_PENDING, res.reason
    assert res.relative[:3] == (EP, "brightness", 1)


def test_too_dim_is_still_ambient_not_relative():
    res = FastPathResolver().resolve("it's too dim in here", reg(), "member", ())
    assert res.status == FALLTHROUGH and res.reason == "relative" or res.status == FALLTHROUGH


async def _run(tmp_path, text, start):
    e = await custom_env(tmp_path, LIGHT)
    try:
        e.fake.state[EP]["brightness"] = start
        ctx, ev = await e.chat(text)
        final = one(ev, "final")
        sets = [c["args"] for c in e.fake.calls if c["tool"] == "set_state" and c["args"]["capability"] == "brightness"]
        return final, sets, e.llm.requests
    finally:
        await e.close()


@pytest.mark.parametrize("text,start,want", [
    ("decrease the test light by 30 percent", 50, 20),
    ("lower the test light by 30", 50, 20),
    ("dim the test light a bit", 50, 40),
    ("make the test light a bit brighter", 50, 60),
    ("turn up the test light by 15", 50, 65),
    ("lower the test light by 30", 10, 0),
    ("raise the test light by 30", 90, 100),
])
async def test_relative_applies_and_clamps(tmp_path, text, start, want):
    final, sets, llm = await _run(tmp_path, text, start)
    assert final["route"] == "fast" and llm == []
    assert sets and sets[-1]["value"] == want
    assert f"{want}%" in final["text"]


async def test_reply_wording(tmp_path):
    final, _, _ = await _run(tmp_path, "lower the test light by 30", 50)
    assert "Test light lowered to 20%" in final["text"]


@pytest.mark.parametrize("text", ["turn on the kitchen fan", "switch off the garage light", "set the kitchen fan to 50%"])
async def test_unknown_device_lists_real_devices(tmp_path, text):
    e = await custom_env(tmp_path, LIGHT)
    try:
        ctx, ev = await e.chat(text)
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        assert final["text"].startswith("I don't see a ") and "I can control: Test light." in final["text"]
        assert not [c for c in e.fake.calls if c["tool"] == "set_state"]
    finally:
        await e.close()


def test_unknown_names_the_user_words():
    res = FastPathResolver().resolve("turn on the kitchen fan", reg(), "member", ())
    assert res.status == FALLTHROUGH and res.unknown == "kitchen fan"


@pytest.mark.parametrize("text", ["turn on the zorp", "turn on the test light and open the kitchen fan",
                                  "turn on the kitchen fan if it is hot", "make it brighter"])
def test_no_device_word_or_mixed_cases_are_not_unknown(text):
    res = FastPathResolver().resolve(text, reg(), "member", ())
    assert res.status == FALLTHROUGH and not res.unknown
