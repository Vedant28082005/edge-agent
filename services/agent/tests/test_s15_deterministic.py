"""S15 phase A: deterministic fixes found in live testing (flag sched.deterministic_fixes). Fixtures follow the real
transcripts: "turn off everything in the bedroom", "close the curtain" (device named Cover), "turn on the light and set it
to 50%" with Light and Light2, "light2" as the clarification answer, "which devices are on right now?", "who are you?"."""
from __future__ import annotations

import pytest
from conftest import one
from test_ambient_polarity import custom_env

from agent.devices import DeviceRegistry
from agent.executor import CallOutcome
from agent.render import describe_question, join_names
from agent.reply import capability_text
from agent.scope import Scope
from agent.sched.config import SchedConfig
from agent.sched.resolver import FALLTHROUGH, PLAN, FastPathResolver

POWER = {"id": "power", "kind": "actuator", "type": "bool"}
BRIGHT = {"id": "brightness", "kind": "actuator", "type": "int", "min": 0, "max": 100}
SPEED = {"id": "speed", "kind": "actuator", "type": "int", "min": 1, "max": 5}
POSITION = {"id": "position", "kind": "actuator", "type": "int", "min": 0, "max": 100}


def dev(n, name, room, typ, caps, risk="normal"):
    return {"endpoint_id": f"ep_c000000{n}", "name": name, "room": room, "type": typ, "tags": [], "online": True,
            "risk": risk, "capabilities": caps}


# A bedroom with two lights (Light, Light2), a fan, a cover named "Cover" (a curtain), and a risk:confirm heater plug
BEDROOM = [
    dev(1, "Light", "Bedroom", "light", [POWER, BRIGHT]),
    dev(2, "Light2", "Bedroom", "light", [POWER, BRIGHT]),
    dev(3, "Fan", "Bedroom", "fan", [POWER, SPEED]),
    dev(4, "Cover", "Bedroom", "cover", [POSITION]),
    dev(5, "Heater plug", "Bedroom", "outlet", [POWER], risk="confirm"),
    dev(6, "Hall light", "Hall", "light", [POWER]),
]


async def env(tmp_path, devices=BEDROOM, **sched):
    sched = {"route_mode": "hybrid", "fast_path": True, "router": False, "deterministic_fixes": True, **sched}
    return await custom_env(tmp_path, devices, **sched)


def sets(e):
    return [(c["args"]["endpoint_id"], c["args"]["capability"], c["args"]["value"]) for c in e.fake.calls if c["tool"] == "set_state"]


# --------------------------------------------------------------------------------------------- A2 room / group scope
async def test_everything_in_the_bedroom(tmp_path):
    e = await env(tmp_path)
    try:
        _, ev = await e.chat("turn off everything in the bedroom")
        f = one(ev, "final")
        assert sorted(sets(e)) == [("ep_c0000001", "power", False), ("ep_c0000002", "power", False), ("ep_c0000003", "power", False)]
        assert "Light" in f["text"] and "Fan" in f["text"] and "Hall light" not in f["text"]
        assert "left out Heater plug" in f["text"] and "confirmation" in f["text"]       # risk:confirm is reported, not done
        assert e.llm.requests == [] and f["route"] == "fast"
    finally:
        await e.close()


async def test_everything_whole_home_and_all_the_type(tmp_path):
    e = await env(tmp_path)
    try:
        await e.chat("turn on everything")
        assert {s[0] for s in sets(e)} == {"ep_c0000001", "ep_c0000002", "ep_c0000003", "ep_c0000006"}
        e.fake.calls.clear()
        _, ev = await e.chat("turn off all the lights")
        assert {s[0] for s in sets(e)} == {"ep_c0000001", "ep_c0000002", "ep_c0000006"} and e.llm.requests == []
        assert "Light, Light2 and Hall light" in one(ev, "final")["text"]            # no "and 1 more" for a short list
    finally:
        await e.close()


async def test_group_by_type_leaves_out_confirm_endpoints(tmp_path):
    e = await env(tmp_path, devices=[*BEDROOM, dev(7, "Kettle plug", "Hall", "outlet", [POWER])])
    try:
        _, ev = await e.chat("turn off all the plugs")
        assert sets(e) == [("ep_c0000007", "power", False)]
        assert "left out Heater plug" in one(ev, "final")["text"]
    finally:
        await e.close()


async def test_unknown_room_is_said_not_guessed(tmp_path):
    e = await env(tmp_path)
    try:
        _, ev = await e.chat("turn off everything in the kitchen")
        assert "kitchen" in one(ev, "final")["text"] and not sets(e)
    finally:
        await e.close()


async def test_guest_never_sets_state_for_a_group(tmp_path):
    e = await env(tmp_path)
    try:
        _, ev = await e.chat("turn off everything in the bedroom", role="guest", scope=Scope(frozenset({"Bedroom"}), frozenset()))
        assert "can't control" in one(ev, "final")["text"] and not sets(e)
    finally:
        await e.close()


def test_flag_off_keeps_the_old_behaviour():
    reg = DeviceRegistry()
    reg.replace(BEDROOM, 1)
    off = FastPathResolver(SchedConfig(deterministic_fixes=False))
    assert off.resolve("turn off everything in the bedroom", reg).status == FALLTHROUGH
    assert off.resolve("close the curtain", reg).status == FALLTHROUGH      # 0.8 * 0.95 < threshold


async def test_flag_off_asks_nothing(tmp_path):
    e = await env(tmp_path, deterministic_fixes=False)
    try:
        _, ev = await e.chat("turn on the light")
        assert "Which one" not in one(ev, "final")["text"]                    # the LLM path, exactly as before S15
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- A1 synonyms
@pytest.mark.parametrize("text", ["close the curtain", "close the curtains", "close the blinds", "close the shades", "close the shade"])
async def test_curtain_blinds_shades_are_the_cover(tmp_path, text):
    e = await env(tmp_path)
    try:
        _, ev = await e.chat(text)
        assert sets(e) == [("ep_c0000004", "position", 0)] and e.llm.requests == []
        assert "Cover" in one(ev, "final")["text"]
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- A3 + A4 ambiguity and its answer
async def test_singular_light_with_two_lights_asks_then_light2_commands_light2(tmp_path):
    e = await env(tmp_path)
    try:
        _, ev = await e.chat("turn on the light and set it to 50%")
        q = one(ev, "final")["text"]
        assert q.startswith("Which one do you mean:") and "Light2" in q
        assert not sets(e) and e.llm.requests == []
        _, ev = await e.chat("light2")
        f = one(ev, "final")["text"]
        assert sets(e) == [("ep_c0000002", "power", True), ("ep_c0000002", "brightness", 50)]
        assert "Light2" in f and "50" in f and not f.startswith("Light on") and "Light turned" not in f
        assert e.llm.requests == []
    finally:
        await e.close()


async def test_clarification_answer_by_ordinal_both_and_unrelated_text(tmp_path):
    e = await env(tmp_path, devices=BEDROOM[:3])
    try:
        await e.chat("turn off the light")
        await e.chat("the second one")
        assert sets(e) == [("ep_c0000002", "power", False)]
        e.fake.calls.clear()
        await e.chat("turn on the light")
        await e.chat("both")
        assert sorted(sets(e)) == [("ep_c0000001", "power", True), ("ep_c0000002", "power", True)]
        e.fake.calls.clear()
        await e.chat("turn on the light")
        _, ev = await e.chat("turn off the fan")                      # not an answer: a new request, nothing hijacked
        assert sets(e) == [("ep_c0000003", "power", False)]
    finally:
        await e.close()


async def test_all_the_lights_still_means_all_and_is_never_asked(tmp_path):
    e = await env(tmp_path, devices=BEDROOM[:3])
    try:
        _, ev = await e.chat("turn on all the lights")
        assert {s[0] for s in sets(e)} == {"ep_c0000001", "ep_c0000002"} and "Which one" not in one(ev, "final")["text"]
    finally:
        await e.close()


async def test_ambiguity_is_asked_on_the_llm_path_too(tmp_path):
    e = await env(tmp_path, devices=BEDROOM[:3], route_mode="llm_first")
    try:
        _, ev = await e.chat("turn on the light")
        assert one(ev, "final")["text"].startswith("Which one do you mean") and e.llm.requests == [] and not sets(e)
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- A4 replies
async def test_value_reply_says_what_was_set(tmp_path):
    e = await env(tmp_path)
    try:
        _, ev = await e.chat("set the fan to 3")
        assert one(ev, "final")["text"] == "Fan speed set to 3."
    finally:
        await e.close()


async def test_which_devices_are_on_lists_only_those_that_are_on(tmp_path):
    for mode in ("rules_first", "hybrid"):              # fast path, and the LLM path (the model reads the whole room)
        e = await env(tmp_path, route_mode=mode)
        try:
            await e.chat("turn on the fan")
            await e.chat("turn on the hall light")
            e.llm.script.append((r"which devices", {"calls": [{"tool": "get_state", "args": {"room": "Bedroom"}}], "say": ""}))
            _, ev = await e.chat("which devices are on right now?")
            t = one(ev, "final")["text"]
            assert "Fan" in t and "Hall light" in t and "Light2" not in t and "Cover" not in t, (mode, t)
            assert "right now: Fan (Bedroom) and Hall light (Hall)" in t
        finally:
            await e.close()


def test_describe_question_list_none_on_and_no_trailing_more():
    reg = DeviceRegistry()
    reg.replace(BEDROOM, 1)
    out = CallOutcome("c1", "get_state", {"room": "Bedroom"}, {"states": [
        {"endpoint_id": "ep_c0000001", "online": True, "values": {"power": False}}]}, True, "done")
    assert describe_question([out], reg, ("list", "power", True, None)) == "Nothing is on."
    assert join_names(["A", "B", "C", "D"]) == "A, B, C and D"
    assert "more" not in join_names([str(i) for i in range(8)])
    assert join_names([str(i) for i in range(10)]).endswith("and 2 more")


async def test_identity_text_is_built_from_the_real_device_types(tmp_path):
    e = await env(tmp_path, route_mode="hybrid")
    try:
        _, ev = await e.chat("who are you?")
        t = one(ev, "final")["text"]
        assert "lights" in t and "fans" in t and "lock" not in t.lower() and e.llm.requests == []
    finally:
        await e.close()


def test_capability_text_follows_types_and_scenes():
    reg = DeviceRegistry()
    reg.replace([dev(1, "Front door", "Hall", "lock", [{"id": "lock", "kind": "actuator", "type": "enum", "values": ["locked", "unlocked"]}])], 1)
    assert "locks" in capability_text(reg)
    reg.replace(BEDROOM, 1)
    assert "lock" not in capability_text(reg) and "run scenes" not in capability_text(reg)
    assert "no devices" not in capability_text(DeviceRegistry()) and "control" not in capability_text(DeviceRegistry())
