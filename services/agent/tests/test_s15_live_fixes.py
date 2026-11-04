"""S15 fixes from the live v20 traces (#145-#154): the chat call answered in JSON again; the planner re-evaluated ~150 tokens of history per
turn; "I'm feeling hot" with a Fan got the generic text; "which devices are on" went to the LLM and listed one device twice; "light2" after a
clarification was passed to the model, which acted on a guess."""
from __future__ import annotations

import re

from conftest import one
from test_s15_chat import LAMP_ONLY, THERMO, chat_env, last_trace, sets
from test_s15_deterministic import env as det_env
from test_s15_deterministic import dev, POWER, BRIGHT

from agent.devices import DeviceRegistry
from agent.executor import CallOutcome
from agent.render import describe_question, device_labels
from agent.schema import ambient_domain_devices
from agent.sched.config import SchedConfig
from agent.util import est_tokens

LIGHTS = [dev(1, "Light", "Bedroom", "light", [POWER, BRIGHT]), dev(2, "Test light", "Hall", "light", [POWER, BRIGHT]),
          dev(3, "Fan", "Bedroom", "fan", [POWER])]


# --------------------------------------------------------------------------------------------- 1. chat text
async def test_a_chat_reply_in_json_is_rejected_with_its_raw_text_in_the_trace(tmp_path):
    e = await chat_env(tmp_path)
    try:
        e.llm.chat_text = '[["chat"]]'                                           # trace #145: 5 tokens, the planner's own format again
        _, ev = await e.chat("hello, how are you?")
        t = last_trace(e)
        assert t["chat_raw"] == '[["chat"]]' and t["say_verdict"] == "chat_json"
        assert "smart-home assistant" in one(ev, "final")["text"]
        e.llm.chat_text = "I'm fine, thanks! How can I help?"
        await e.chat("hello, how are you?")
        t = last_trace(e)
        assert t["chat_raw"] == "I'm fine, thanks! How can I help?" and t["say_verdict"] == "chat_ok"
    finally:
        await e.close()


async def test_the_chat_request_leads_with_plain_words_and_replays_no_json_answer(tmp_path):
    e = await chat_env(tmp_path)
    try:
        await e.chat("hello, how are you?")
        plan_req, chat_req = e.llm.requests
        assert chat_req[0] == plan_req[0] and len(chat_req) == 2                  # same system message; ONE final user message
        assert not [m for m in chat_req if m["role"] == "assistant"]
        assert chat_req[1]["content"].startswith("Chat reply: plain words, no JSON.")
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- 2. context instead of history
async def test_the_planner_tail_is_a_context_line_not_history_and_it_still_resolves_it(tmp_path):
    e = await chat_env(tmp_path, devices=LIGHTS)
    try:
        al = e.agent.builder.aliases

        def power_off_it(messages):                                              # a model that reads "last devices: e3 Fan" off the line
            ctx = re.search(r"last devices: (e\d+)", messages[-1]["content"])
            return {"calls": [{"tool": "set_state", "args": {"endpoint_id": al.to_ep[ctx.group(1)], "capability": "power", "value": False}}]}

        e.llm.script.append((r"turn on the fan", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000003", "capability": "power", "value": True}}]}))
        e.llm.script.append((r"turn it off", power_off_it))
        await e.chat("turn on the fan")
        first = e.llm.requests[0]
        assert [m["role"] for m in first] == ["system", "user"] and first[1]["content"] == "User: turn on the fan"
        await e.chat("turn it off")
        second = e.llm.requests[1]
        assert [m["role"] for m in second] == ["system", "user"], "no history messages"
        assert second[0] == first[0]                                              # the cached prefix is untouched
        tail = second[1]["content"]
        assert tail.startswith("Context: last devices: ") and "Fan" in tail and "last action: turned on" in tail
        assert sum(est_tokens(m["content"]) for m in second[1:]) <= 40, tail    # [est] 1 token / 3 bytes, conservative
        assert sets(e) == [("ep_c0000003", "power", True), ("ep_c0000003", "power", False)]       # "it" was the fan
        t = last_trace(e)
        assert t["steps"][0]["prompt_n"] <= 40
    finally:
        await e.close()


async def test_the_context_line_carries_a_question_and_expires(tmp_path, monkeypatch):
    e = await chat_env(tmp_path, devices=[*LAMP_ONLY, THERMO])
    try:
        e.llm.script.append((r"hot", {"calls": []}))
        await e.chat("I'm hot")                                                  # -> "Which one do you mean ...?"
        await e.chat("what is the weather like in paris today for my trip please")
        assert "asked: Which one do you mean" in e.llm.requests[-1][1]["content"]
        import agent.chat as chatmod
        monkeypatch.setattr(chatmod, "CONTEXT_TTL_S", -1.0)
        await e.chat("hello there")
        assert e.llm.requests[-2][1]["content"] == "User: hello there"      # [-1] is its chat call
    finally:
        await e.close()


async def test_classic_prompt_keeps_its_history(tmp_path):
    from test_ambient_polarity import custom_env
    e = await custom_env(tmp_path, LAMP_ONLY, route_mode="llm_first")
    try:
        await e.chat("hello")
        await e.chat("hello again")
        assert len(e.llm.requests[1]) > 2                                        # system + history + user, exactly as before S15
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- 3. comfort phrases
def test_hot_is_also_answered_by_a_fan_but_only_in_the_wide_mode():
    from agent.devices import DeviceRegistry as R
    reg = R()
    reg.replace([*LAMP_ONLY, THERMO], 1)
    assert [d.name for d in ambient_domain_devices(reg, "temp", -1)] == ["Thermostat"]
    assert sorted(d.name for d in ambient_domain_devices(reg, "temp", -1, True)) == ["Fan", "Thermostat"]
    assert [d.name for d in ambient_domain_devices(reg, "temp", 1, True)] == ["Thermostat"]          # "cold": never the fan


async def test_im_feeling_hot_with_a_fan_proposes_the_fan_even_when_the_model_says_nothing(tmp_path):
    e = await chat_env(tmp_path, devices=LAMP_ONLY)
    try:
        e.llm.script.append((r"hot", {"calls": []}))                              # trace #153: raw '[]'
        _, ev = await e.chat("I'm feeling hot")
        assert one(ev, "final")["text"] == "Want me to turn on Fan?" and not sets(e)
        await e.chat("yes")
        assert sets(e) == [("ep_c0000002", "power", True)]
    finally:
        await e.close()


async def test_hot_with_a_thermostat_and_a_fan_asks_and_the_answer_decides(tmp_path):
    e = await chat_env(tmp_path, devices=[*LAMP_ONLY, THERMO], deterministic_fixes=True)
    try:
        e.llm.script.append((r"hot", {"calls": []}))
        _, ev = await e.chat("I'm feeling hot")
        q = one(ev, "final")["text"]
        assert q.startswith("Which one do you mean") and "Thermostat" in q and "Fan" in q
        _, ev = await e.chat("toaster")                                           # names neither: asked again, not guessed
        assert "among those" in one(ev, "final")["text"] and not sets(e)
        _, ev = await e.chat("the fan")
        assert sets(e) == [("ep_c0000002", "power", True)] and "Fan" in one(ev, "final")["text"]
    finally:
        await e.close()


async def test_cold_goes_to_the_thermostat_and_nothing_to_answer_means_chat(tmp_path):
    e = await chat_env(tmp_path, devices=[*LAMP_ONLY, THERMO])
    try:
        e.llm.script.append((r"cold", {"calls": []}))
        _, ev = await e.chat("I'm cold")
        assert one(ev, "final")["text"] == "Want me to set Thermostat to heat?"
    finally:
        await e.close()
    e = await chat_env(tmp_path, devices=[LAMP_ONLY[0]])
    try:
        e.llm.script.append((r"hot", {"calls": []}))
        _, ev = await e.chat("I'm feeling hot")
        t = one(ev, "final")["text"]
        assert "couldn't work out" not in t and t == "Hi! I can help with your lights and fans."
        assert last_trace(e)["say_verdict"] == "chat_ok" and not sets(e)
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- 4. status in hybrid, labels
def test_status_joins_the_hybrid_fast_intents_only_with_the_flag():
    assert "status" not in SchedConfig.from_dict({}).fast_intents                    # unchanged without the flag
    c = SchedConfig.from_dict({"deterministic_fixes": True})
    assert c.fast_intents[-1] == "status" and c.allows_fast("status") and not c.allows_fast("ambient")
    assert SchedConfig.from_dict({"deterministic_fixes": True, "fast_intents": ["command"]}).fast_intents == ["command"]   # explicit wins


async def test_which_devices_are_on_is_answered_from_the_registry_without_the_llm(tmp_path):
    e = await det_env(tmp_path, devices=LIGHTS, route_mode="hybrid")
    try:
        await e.chat("turn on the fan")
        await e.chat("turn on the test light")
        e.llm.requests.clear()
        _, ev = await e.chat("which devices are on right now?")
        f = one(ev, "final")
        assert f["text"] == "On right now: Fan (Bedroom) and Test light (Hall)." and f["route"] == "fast" and e.llm.requests == []
    finally:
        await e.close()


def test_listing_never_repeats_a_device_and_tells_same_named_ones_apart():
    reg = DeviceRegistry()
    reg.replace([dev(1, "Light", "Bedroom", "light", [POWER]), dev(2, "Light", "Bedroom", "light", [POWER]),
                 dev(3, "Light", "Bedroom", "outlet", [POWER]), dev(4, "Fan", "Bedroom", "fan", [POWER])], 1)
    st = lambda ep: {"endpoint_id": f"ep_c000000{ep}", "online": True, "values": {"power": True}}          # noqa: E731
    same_room_twice = [CallOutcome("c1", "get_state", {"room": "Bedroom"}, {"states": [st(1), st(2), st(4)]}, True, "done"),
                       CallOutcome("c2", "get_state", {"endpoint_id": "ep_c0000001"}, {"states": [st(1)]}, True, "done")]
    t = describe_question(same_room_twice, reg, ("list", "power", True, None))
    assert t.count("Fan (Bedroom)") == 1 and t.count("Light (Bedroom)") == 2 and "#1" in t and "#2" in t, t
    labels = device_labels([reg.devices["ep_c0000001"], reg.devices["ep_c0000003"]], with_room=True)
    assert labels == ["Light (Bedroom) [light]", "Light (Bedroom) [outlet]"]


# --------------------------------------------------------------------------------------------- 5. unanswered clarification
async def test_a_reply_that_matches_neither_option_is_asked_again_never_passed_to_the_model(tmp_path):
    e = await det_env(tmp_path, devices=LIGHTS, route_mode="hybrid")
    try:
        _, ev = await e.chat("turn on the light and set it to 50%")
        q = one(ev, "final")["text"]
        assert q == "Which one do you mean: Light or Test light?"
        _, ev = await e.chat("light2")                                            # trace #151
        again = one(ev, "final")["text"]
        assert again == 'I don\'t see "light2" among those. Which one do you mean: Light or Test light?'
        assert e.llm.requests == [] and not sets(e)
        _, ev = await e.chat("light3")
        assert "among those" in one(ev, "final")["text"] and not sets(e)
        _, ev = await e.chat("light4")                                            # not forever
        assert "leave that" in one(ev, "final")["text"] and not sets(e)
        _, ev = await e.chat("light2")                                            # the question is gone: an ordinary message now
        assert not [s for s in sets(e)]
    finally:
        await e.close()


async def test_the_answer_after_a_re_ask_still_works_and_cancel_cancels(tmp_path):
    e = await det_env(tmp_path, devices=LIGHTS, route_mode="hybrid")
    try:
        await e.chat("turn on the light and set it to 50%")
        await e.chat("light2")
        _, ev = await e.chat("test light")
        assert sets(e) == [("ep_c0000002", "power", True), ("ep_c0000002", "brightness", 50)] and "Test light" in one(ev, "final")["text"]
        e.fake.calls.clear()
        await e.chat("turn off the light")
        _, ev = await e.chat("never mind")
        assert "cancelled" in one(ev, "final")["text"] and not sets(e)
        _, ev = await e.chat("first")                                             # nothing pending any more
        assert not sets(e)
    finally:
        await e.close()


async def test_the_model_never_writes_on_the_turn_after_an_unanswered_clarification(tmp_path):
    e = await det_env(tmp_path, devices=LIGHTS, route_mode="hybrid")
    try:
        e.llm.script.append((r"anything", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000001", "capability": "power", "value": True}}]}))
        await e.chat("turn on the light")
        _, ev = await e.chat("could you do anything at all that you think is best for this room right now")      # long: a new request
        assert not sets(e) and "changed nothing" in one(ev, "final")["text"] and len(e.llm.requests) == 1
        # the next turn is ordinary again: the model may write
        e.llm.script.append((r"model please", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000003", "capability": "power", "value": True}}]}))
        await e.chat("model please")
        assert sets(e) == [("ep_c0000003", "power", True)]
    finally:
        await e.close()
    e = await det_env(tmp_path, devices=LIGHTS, route_mode="llm_first")           # no fast path at all: a recognisable command is the model's too
    try:
        e.llm.script.append((r"fan", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000003", "capability": "power", "value": False}}]}))
        await e.chat("turn on the light")
        _, ev = await e.chat("turn off the fan")
        assert not sets(e) and "changed nothing" in one(ev, "final")["text"]
    finally:
        await e.close()
