"""S15 phase C: the chat route (flag sched.chat_route, default off; needs generated_prompt). The planner may answer [["chat"]]; one
short unconstrained call then follows on the SAME cached prefix (byte-identical system message), so a chat turn never makes the
next planner turn re-evaluate the prefix. Conversation turns and reads never write; "yes"/"thanks" alone never execute; comfort
phrases end in one proposal or one question."""
from __future__ import annotations

import copy

import pytest
from conftest import one
from test_ambient_polarity import custom_env
from test_s15_deterministic import BEDROOM, POWER, dev
from test_s15_prompt import many, reg_of
from gbnf_regex import to_regex

from agent.aliases import build_aliases
from agent.llm import LlmBusy
from agent.planner import parse_plan
from agent.prompt import PromptBuilder
from agent.reply import ack_reply, capability_text, chat_message, finish_chat_text, is_bare_ack
from agent.roster import TEMPLATE, TEMPLATE_CHAT
from agent.sched.config import SchedConfig
from agent.sched.grammar import ConstraintCache
from agent.util import est_tokens

THERMO = dev(7, "Thermostat", "Bedroom", "thermostat",
             [{"id": "mode", "kind": "actuator", "type": "enum", "values": ["heat", "cool", "off"]}])
LAMP_ONLY = [dev(1, "Lamp", "Bedroom", "light", [POWER]), dev(2, "Fan", "Bedroom", "fan", [POWER])]


def sets(e):
    return [(c["args"]["endpoint_id"], c["args"]["capability"], c["args"]["value"]) for c in e.fake.calls if c["tool"] == "set_state"]


async def chat_env(tmp_path, devices=BEDROOM, **sched):
    sched = {"route_mode": "llm_first", "generated_prompt": True, "chat_route": True, **sched}
    e = await custom_env(tmp_path, copy.deepcopy(devices), **sched)
    e.llm.registry, e.llm.chat_mode = e.agent.registry, True
    e.spy = []
    orig = e.llm.complete

    async def spy(messages, schema, max_tokens=None, repeat_penalty=None):
        e.spy.append({"constrained": schema is not None, "max_tokens": max_tokens})
        return await orig(messages, schema, max_tokens, repeat_penalty)

    e.llm.complete = spy
    return e


def last_trace(e):
    t = e.agent.store.traces(1)[0]
    return t["detail"] if "detail" in t else t


# --------------------------------------------------------------------------------------------- config / grammar / parsing
def test_flags_default_off_and_chat_needs_the_generated_prompt():
    c = SchedConfig()
    assert c.chat_route is False and c.generated_prompt is False and c.chat_max_tokens == 40
    with pytest.raises(ValueError):
        SchedConfig.from_dict({"chat_route": True})
    assert SchedConfig.from_dict({"chat_route": True, "generated_prompt": True}).chat_route


def test_the_grammar_offers_chat_only_when_the_flag_is_on():
    reg = reg_of(BEDROOM)
    on = ConstraintCache().get(reg, None, "", 0, True)
    off = ConstraintCache().get(reg, None, "", 0, False)
    assert to_regex(on.gbnf).fullmatch('[["chat"]]') and not to_regex(on.gbnf).fullmatch('[["chat2"]]')
    assert not to_regex(off.gbnf).fullmatch('[["chat"]]')
    assert to_regex(on.gbnf).fullmatch('[["s","e1","power",true]]')                      # acting is still possible
    assert on.key != off.key


def test_parse_plan_chat_is_only_the_first_element_and_drops_the_rest():
    al = build_aliases(reg_of(BEDROOM))
    p = parse_plan('[["chat"]]', al)
    assert p.chat and p.calls == [] and p.parsed_ok
    q = parse_plan('[["chat"],["s","e1","power",true]]', al)
    assert q.chat and q.calls == []                                                      # never act next to a chat decision
    r = parse_plan('[["s","e1","power",true],["chat"]]', al)
    assert not r.chat and len(r.calls) == 1


def test_template_explains_chat_and_stays_small():
    assert "[[\"chat\"]]" in TEMPLATE_CHAT and "chat" not in TEMPLATE                    # the flag-off template is untouched
    b = PromptBuilder(reg_of(many(10)), generated=True, chat=True)
    assert b.prefix.startswith(TEMPLATE_CHAT) and est_tokens(b.prefix) < 700
    assert len([ln for ln in TEMPLATE_CHAT.splitlines() if ln.startswith("U:")]) == 8


def test_finish_chat_text_keeps_two_complete_sentences():
    assert finish_chat_text("Hello there! I can help. And more.") == "Hello there! I can help."
    assert finish_chat_text("Hello there! I can hel") == "Hello there!"
    assert finish_chat_text("I can hel") == "" and finish_chat_text("") == ""
    assert is_bare_ack("thanks") and is_bare_ack("yes") and is_bare_ack("ok thanks a lot") and not is_bare_ack("yes turn on the fan")
    assert ack_reply("thanks") == "You're welcome!" and ack_reply("yes").startswith("OK")


# --------------------------------------------------------------------------------------------- C1/C2: the chat turn
async def test_chat_turn_answers_in_words_and_shares_the_planners_prefix(tmp_path):
    e = await chat_env(tmp_path)
    try:
        _, ev = await e.chat("hello there")
        f = one(ev, "final")
        assert f["text"] == "Hi! I can help with your lights and fans." and f["route"] == "llm"
        assert not sets(e) and [c for c in e.fake.calls if c["tool"] != "list_devices"] == []
        plan_req, chat_req = e.llm.requests
        assert e.spy[0]["constrained"] is True and e.spy[1] == {"constrained": False, "max_tokens": 40}   # unconstrained, ~40 tokens
        # the shared-prefix property: the system message (template + roster) is byte-identical to the planner's. The chat request is that
        # system message plus ONE user message that starts with the plain-words instruction (no replayed `[["chat"]]` answer, which made
        # the model answer in JSON again: trace #145)
        assert chat_req[0] == plan_req[0] and chat_req[0]["content"] == e.agent.builder.prefix
        assert len(chat_req) == 2 and chat_req[1]["role"] == "user"
        msg = chat_req[1]["content"]
        assert msg.startswith("Chat reply: plain words, no JSON") and msg.endswith("Message: hello there")
        assert est_tokens(msg) <= 80 and "You can control" in msg
        t = last_trace(e)
        assert t["decision"] == "chat" and t["roster_hash"] == e.agent.builder.roster_hash and t["llm_calls"] == 2
        assert [s["step"] for s in t["steps"]] == ["plan", "chat"] and t["stage_ms"]["chat_ms"] >= 0
    finally:
        await e.close()


async def test_a_chat_turn_never_makes_the_next_planner_turn_re_evaluate_the_prefix(tmp_path):
    e = await chat_env(tmp_path)
    try:
        prefix_tokens = est_tokens(e.agent.builder.prefix)
        await e.chat("hello there")
        t1 = last_trace(e)
        plan_step, chat_step = t1["steps"]
        # the chat call evaluated only its tail (assistant decision + instruction); the whole planner request came from the cache
        tail_est = est_tokens(chat_message(e.agent.registry, "hello there")) + 8
        assert chat_step["cache_n"] >= prefix_tokens and chat_step["prompt_n"] <= tail_est, chat_step
        e.llm.script.append((r"fan", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000003", "capability": "power", "value": True}}], "say": ""}))
        await e.chat("start the fan")
        t2 = last_trace(e)
        assert t2["decision"] == "act" and sets(e) == [("ep_c0000003", "power", True)]
        s = t2["steps"][0]
        assert s["cache_n"] >= prefix_tokens - 2, s                     # the planner found the template + roster already evaluated
        assert s["prompt_n"] < prefix_tokens / 2, s                     # ... and evaluated only the new turn
        assert t2["cache_hit_tokens"] == s["cache_n"] and t2["prompt_eval_tokens"] == s["prompt_n"]
        assert e.llm.requests[-1][0] == e.llm.requests[0][0]            # one system message for the whole conversation
    finally:
        await e.close()


async def test_chat_text_is_validated_and_falls_back_to_the_capability_line(tmp_path):
    e = await chat_env(tmp_path)
    try:
        cap = capability_text(e.agent.registry)
        for bad in ("I turned on the light for you.", "Sure thing", "", "It is 23 degrees in here."):
            e.llm.chat_text = bad
            _, ev = await e.chat("hello there")
            assert one(ev, "final")["text"] == cap, bad
            assert last_trace(e)["say_verdict"].startswith("chat_") and last_trace(e)["say_verdict"] != "chat_ok"
        e.llm.chat_text = "Hello! I can control your lights. More text."
        _, ev = await e.chat("hello there")
        assert one(ev, "final")["text"] == "Hello! I can control your lights." and last_trace(e)["say_verdict"] == "chat_ok"
        assert not sets(e)
    finally:
        await e.close()


async def test_a_busy_model_during_the_chat_call_gives_the_capability_line(tmp_path):
    e = await chat_env(tmp_path)
    try:
        orig = e.llm.complete

        async def flaky(messages, schema, max_tokens=None, repeat_penalty=None):
            if schema is None:
                raise LlmBusy("busy")
            return await orig(messages, schema, max_tokens, repeat_penalty)

        e.llm.complete = flaky
        _, ev = await e.chat("hello there")
        assert one(ev, "final")["text"] == capability_text(e.agent.registry)
    finally:
        await e.close()


async def test_flag_off_keeps_the_old_behaviour(tmp_path):
    e = await custom_env(tmp_path, BEDROOM, route_mode="llm_first", generated_prompt=True)
    e.llm.registry, e.llm.chat_mode = e.agent.registry, True
    try:
        _, ev = await e.chat("hello there")
        assert len(e.llm.requests) == 1 and "[\"chat\"]" not in e.llm.requests[0][0]["content"]       # no chat call, no chat rule
        assert last_trace(e)["decision"] == ""
    finally:
        await e.close()


async def test_route_modes_still_work_with_the_chat_route_on(tmp_path):
    for mode in ("hybrid", "rules_first"):
        e = await chat_env(tmp_path, route_mode=mode, fast_path=True)
        try:
            await e.chat("turn on the fan")
            assert sets(e) == [("ep_c0000003", "power", True)] and e.llm.requests == []
        finally:
            await e.close()


# --------------------------------------------------------------------------------------------- C3: nothing executes from small talk
async def test_thanks_and_yes_alone_never_execute_and_cost_no_llm_call(tmp_path):
    e = await chat_env(tmp_path)
    try:
        await e.chat("turn on the fan") if False else None
        for text, want in (("thanks", "You're welcome!"), ("thank you so much", "You're welcome!"), ("yes", "OK. Tell me what to turn on or off."),
                           ("ok", "OK. Tell me what to turn on or off.")):
            _, ev = await e.chat(text)
            assert one(ev, "final")["text"] == want
        assert e.llm.requests == [] and not sets(e)
        assert last_trace(e)["decision"] == "chat"
    finally:
        await e.close()


async def test_a_model_that_writes_during_a_question_is_still_stripped(tmp_path):
    e = await chat_env(tmp_path)
    try:
        e.llm.script.append((r"is the fan", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000003", "capability": "power", "value": False}},
                                                        {"tool": "get_state", "args": {"endpoint_id": "ep_c0000003"}}], "say": ""}))
        await e.chat("is the fan on?")
        assert not sets(e)
    finally:
        await e.close()


# --------------------------------------------------------------------------------------------- C4: comfort phrases
async def test_comfort_phrase_with_one_candidate_is_a_proposal_and_a_yes_executes_it(tmp_path):
    e = await chat_env(tmp_path, devices=LAMP_ONLY)
    try:
        e.llm.script.append((r"dark", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000001", "capability": "power", "value": True}}], "say": ""}))
        _, ev = await e.chat("it's too dark in here")
        assert one(ev, "final")["text"] == "Want me to turn on Lamp?" and not sets(e)              # the model's guess is not executed
        _, ev = await e.chat("yes")
        assert sets(e) == [("ep_c0000001", "power", True)] and "Lamp" in one(ev, "final")["text"]
    finally:
        await e.close()


async def test_comfort_phrase_the_model_calls_chat_is_still_a_proposal_or_question(tmp_path):
    e = await chat_env(tmp_path, devices=[*LAMP_ONLY, THERMO])           # a thermostat AND a fan can answer "hot"
    try:
        e.llm.script.append((r"hot", {"calls": [], "chat": True}))
        _, ev = await e.chat("I'm hot")
        t = one(ev, "final")["text"]
        assert t.startswith("Which one do you mean") and "Thermostat" in t and "Fan" in t and not sets(e) and len(e.llm.requests) == 1
        _, ev = await e.chat("no thanks")                                                           # declining does nothing
        assert not sets(e)
    finally:
        await e.close()


async def test_comfort_phrase_with_two_candidates_asks_and_with_none_just_chats(tmp_path):
    two = [dev(1, "Lamp", "Bedroom", "light", [POWER]), dev(2, "Light2", "Bedroom", "light", [POWER])]
    e = await chat_env(tmp_path, devices=two)
    try:
        e.llm.script.append((r"dark", {"calls": [], "chat": True}))
        _, ev = await e.chat("too dark")
        assert one(ev, "final")["text"].startswith("Which one do you mean") and not sets(e)
    finally:
        await e.close()
    e = await chat_env(tmp_path, devices=[dev(1, "Lamp", "Bedroom", "light", [POWER])])             # nothing can answer "hot"
    try:
        e.llm.script.append((r"hot", {"calls": [], "chat": True}))
        _, ev = await e.chat("I'm hot")
        assert one(ev, "final")["text"] == "Hi! I can help with your lights and fans." and not sets(e)
    finally:
        await e.close()
