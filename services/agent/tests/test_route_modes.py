"""S14: the LLM as the main brain. route_mode (hybrid | llm_first | rules_first) + fast_intents decide what the no-LLM
fast path may answer; the model may add a short `say` next to its calls; the agent keeps the verified text unless the
model's agrees with the outcomes; read-only questions can be phrased by a second, streamed LLM call."""
from __future__ import annotations

import pytest
from conftest import make_env, names, one

from agent.config import AgentConfig, load_config
from agent.executor import CallOutcome
from agent.mock import demo_devices
from agent.reply import numbers_ok, phrase_verdict, polarity_conflict, say_verdict
from agent.sched.config import SchedConfig


async def menv(tmp_path, mode, say=True, **sched):
    e = await make_env(tmp_path, sched={"route_mode": mode, "router": False, "say_in_plan": True, **sched})
    e.llm.emit_say = say
    return e


def hub_sets(e):
    return [c for c in e.fake.calls if c["tool"] == "set_state"]


def last_trace(e):
    return e.agent.store.traces(1)[0]["detail"]


def tokens(ev):
    return [d["text"] for n, d in ev if n == "token"]


# ------------------------------------------------------------------------------------------------------ config
def test_defaults_and_validation():
    c = SchedConfig.from_dict({})
    assert c.route_mode == "hybrid" and c.fast_intents == ["command", "set_value", "compound", "identity"]
    assert not c.natural_read_replies and not c.say_in_plan and not c.allow_say
    assert SchedConfig.from_dict({"say_in_plan": True}).allow_say
    assert not SchedConfig.from_dict({"say_in_plan": True, "route_mode": "rules_first"}).allow_say
    assert [c.allows_fast(i) for i in ("command", "set_value", "compound", "relative", "status", "list", "identity", "ambient")] == \
           [True, True, True, False, False, False, True, False]
    assert not SchedConfig.from_dict({"route_mode": "rules_first"}).allow_say
    assert all(SchedConfig.from_dict({"route_mode": "rules_first"}).allows_fast(i) for i in ("status", "identity", "list"))
    assert not any(SchedConfig.from_dict({"route_mode": "llm_first", "fast_intents": ["command"]}).allows_fast(i)
                   for i in ("command", "status"))
    for bad in ({"route_mode": "fast"}, {"fast_intents": ["command", "dance"]}, {"fast_intents": "command"},
                {"natural_read_replies": "yes"}, {"say_in_plan": 1}, {"say_max_tokens": 0}):
        with pytest.raises(ValueError):
            SchedConfig.from_dict(bad)


def test_agent_config_loads_route_keys(tmp_path):
    f = tmp_path / "a.json"
    f.write_text('{"sched": {"route_mode": "llm_first", "natural_read_replies": true}}')
    assert load_config(str(f)).sched["route_mode"] == "llm_first"
    f.write_text('{"sched": {"route_mode": "nope"}}')
    with pytest.raises(ValueError):
        load_config(str(f))
    assert AgentConfig().assistant_name == "Edge Home"


# ------------------------------------------------------------------------------------------------------ routing
async def test_hybrid_fast_for_commands_llm_for_everything_else(tmp_path):
    e = await menv(tmp_path, "hybrid")
    try:
        _, ev = await e.chat("turn on the bedroom fan")
        assert one(ev, "final")["route"] == "fast" and e.llm.requests == []
        t = last_trace(e)
        assert t["route_mode"] == "hybrid" and t["text_source"] == "agent"
        _, ev = await e.chat("set the bedroom fan speed to 3")
        assert one(ev, "final")["route"] == "fast" and e.llm.requests == []
        n = 0
        for q in ("is the bedroom fan on?", "what is the device status?", "devices", "it's dark"):
            _, ev = await e.chat(q)
            n += 1
            assert one(ev, "final")["route"] == "llm" and len(e.llm.requests) == n, q
        _, ev = await e.chat("who are you")                     # S15: identity is deterministic registry text, even in hybrid
        assert one(ev, "final")["route"] == "fast" and len(e.llm.requests) == n
    finally:
        await e.close()


async def test_hybrid_fast_intents_can_list_status_identity_list(tmp_path):
    e = await menv(tmp_path, "hybrid", fast_intents=["command", "status", "list", "identity"])
    try:
        for q in ("who are you", "devices", "what is the device status?", "is the bedroom fan on?"):
            _, ev = await e.chat(q)
            assert one(ev, "final")["route"] == "fast", q
        assert e.llm.requests == []
    finally:
        await e.close()


async def test_llm_first_has_no_fast_path_even_for_identity_and_list(tmp_path):
    e = await menv(tmp_path, "llm_first", fast_intents=["command", "list", "identity", "status"])
    try:
        for i, q in enumerate(("turn on the bedroom fan", "who are you", "devices"), 1):
            _, ev = await e.chat(q)
            assert one(ev, "final")["route"] == "llm" and len(e.llm.requests) == i, q
        assert last_trace(e)["route_mode"] == "llm_first"
    finally:
        await e.close()


async def test_rules_first_is_the_old_behaviour(tmp_path):
    e = await menv(tmp_path, "rules_first")
    try:
        for q in ("who are you", "devices", "is the bedroom fan on?", "turn on the bedroom fan"):
            _, ev = await e.chat(q)
            assert one(ev, "final")["route"] == "fast", q
        assert e.llm.requests == []
        assert last_trace(e)["route_mode"] == "rules_first"
    finally:
        await e.close()


# ------------------------------------------------------------------------------------------------------ say shapes
async def test_say_only_identity_answered_from_the_fact_line(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        _, ev = await e.chat("who are you")
        f = one(ev, "final")
        assert f["route"] == "llm" and "Edge Home" in f["text"] and not hub_sets(e)
        sys_prompt = e.llm.requests[0][0]["content"]
        assert "Facts: you are Edge Home" in sys_prompt and "light" in sys_prompt.split("Facts:")[1]
        t = last_trace(e)
        assert t["text_source"] == "model" and t["say_verdict"] == "ok"
        assert "".join(tokens(ev)).strip() == f["text"]
    finally:
        await e.close()


async def test_say_only_answer_from_the_device_list(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        _, ev = await e.chat("how many devices do I have?")
        assert one(ev, "final")["text"] == "You have 6 devices." and last_trace(e)["text_source"] == "model"
    finally:
        await e.close()


async def test_calls_and_say_shows_the_model_text_when_it_agrees(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        _, ev = await e.chat("turn on the living room lamp")
        assert [c["args"]["value"] for c in hub_sets(e)] == [True]
        assert one(ev, "final")["text"] == "Turning on the lamp."
        t = last_trace(e)
        assert t["text_source"] == "model" and t["llm_calls"] == 1 and t["route"] == "llm"
    finally:
        await e.close()


async def test_calls_only_uses_the_agent_text(tmp_path):
    e = await menv(tmp_path, "llm_first", say=False)
    try:
        _, ev = await e.chat("turn on the living room lamp")
        assert one(ev, "final")["text"] == "Lamp turned on." and last_trace(e)["text_source"] == "agent"
    finally:
        await e.close()


@pytest.mark.parametrize("say,code", [
    ("Turning it off.", "polarity"),
    ("Done, turned on the lamp at 80%.", "numbers"),
    ("Sorry, I couldn't do that.", "says_failure_after_success"),
])
async def test_contradicting_say_falls_back_to_the_agent_text(tmp_path, say, code):
    e = await menv(tmp_path, "llm_first")
    try:
        e.llm.script.append((r"lamp", {"calls": [{"tool": "set_state", "args": {
            "endpoint_id": "ep_a1000006", "capability": "power", "value": True}}], "say": say}))
        _, ev = await e.chat("turn on the living room lamp")
        assert one(ev, "final")["text"] == "Lamp turned on."
        t = last_trace(e)
        assert t["text_source"] == "agent" and t["say_verdict"] == code
    finally:
        await e.close()


async def test_say_claiming_an_action_that_did_not_run_is_dropped(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        e.llm.script.append((r"lamp", {"calls": [], "say": "I turned on the lamp."}))
        _, ev = await e.chat("turn on the living room lamp")
        assert not hub_sets(e)
        assert "turned on" not in one(ev, "final")["text"] and last_trace(e)["say_verdict"] == "claims_unrun_action"
        e.llm.script.append((r"light", {"calls": [], "say": "The ceiling light is off."}))
        _, ev = await e.chat("hmm the ceiling light")
        assert last_trace(e)["say_verdict"] == "claims_state_without_read"
    finally:
        await e.close()


async def test_read_turn_ignores_the_model_text_and_reports_the_reading(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        _, ev = await e.chat("what is the temp sensor temperature?")
        f = one(ev, "final")
        assert "24.5" in f["text"] and f["text"] != "Checking that for you."
        assert last_trace(e)["say_verdict"] == "read_turn" and last_trace(e)["text_source"] == "agent"
    finally:
        await e.close()


async def test_rules_first_has_no_say_in_prompt_or_grammar(tmp_path):
    e = await menv(tmp_path, "rules_first", fast_path=False)
    try:
        _, ev = await e.chat("turn on the living room lamp")
        assert "Facts:" not in e.llm.requests[0][0]["content"]
        assert one(ev, "final")["text"] == "Lamp turned on." and last_trace(e)["text_source"] == "agent"
    finally:
        await e.close()


# ------------------------------------------------------------------------------------------------------ guards stay on
async def test_question_gate_still_strips_writes_and_drops_the_say(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        e.llm.script.append((r"lamp", {"calls": [{"tool": "set_state", "args": {
            "endpoint_id": "ep_a1000006", "capability": "power", "value": False}}], "say": "Turned the lamp off."}))
        _, ev = await e.chat("is the lamp on?")
        assert not hub_sets(e) and "Turned" not in one(ev, "final")["text"]
        assert last_trace(e)["say_verdict"] == "stripped_write"
    finally:
        await e.close()


async def test_guest_cannot_control_via_the_llm_either(tmp_path):
    from conftest import GUEST_SCOPE
    e = await menv(tmp_path, "llm_first")
    try:
        _, ev = await e.chat("turn on the living room lamp", user="guest1", role="guest", scope=GUEST_SCOPE)
        assert not hub_sets(e)
        assert one(ev, "final")["text"] != "Turning on the lamp."
        assert last_trace(e)["text_source"] == "agent"
    finally:
        await e.close()


async def test_power_dependency_and_brightness_floor_apply_to_llm_first(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        await e.chat("dim the ceiling light to 40")
        assert [(c["args"]["capability"], c["args"]["value"]) for c in hub_sets(e)] == [("power", True), ("brightness", 40)]
    finally:
        await e.close()


def test_unconfirmed_or_failed_outcomes_never_use_the_model_text():
    args = {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"}
    for state in ("declined", "blocked", "expired", "failed", "rejected"):
        out = CallOutcome("c1", "set_state", args, {}, False, state)
        assert say_verdict("Unlocking the front door.", [out], None, "", "unlock the door") == "outcome_not_ok"


# ------------------------------------------------------------------------------------------------------ natural reads
async def test_natural_read_reply_streams_tokens_and_is_the_final_text(tmp_path):
    e = await menv(tmp_path, "llm_first", natural_read_replies=True)
    try:
        _, ev = await e.chat("what is the temp sensor temperature?")
        f = one(ev, "final")
        assert f["text"] == "Right now: Temp sensor temperature: 24.5C."
        toks = tokens(ev)
        assert len(toks) > 3 and "".join(toks) == f["text"]
        assert names(ev).index("token") < names(ev).index("final")
        t = last_trace(e)
        assert t["text_source"] == "model" and t["llm_calls"] == 2 and t["say_verdict"] == "phrase_ok"
        assert len(e.llm.requests) == 1 and len(e.llm.phrase_requests) == 1
        msg = e.llm.phrase_requests[0][-1]["content"]
        assert "24.5" in msg and "ep_a" not in msg                        # facts only, no endpoint ids
    finally:
        await e.close()


async def test_natural_read_reply_with_a_wrong_number_falls_back_to_the_agent_text(tmp_path):
    e = await menv(tmp_path, "llm_first", natural_read_replies=True)
    try:
        e.llm.phrase = lambda facts, q: "It is a toasty 31 degrees in the bedroom."
        _, ev = await e.chat("what is the temp sensor temperature?")
        f = one(ev, "final")
        assert "24.5" in f["text"] and "31" not in f["text"]
        t = last_trace(e)
        assert t["text_source"] == "agent" and t["say_verdict"] == "phrase_numbers"
    finally:
        await e.close()


async def test_natural_read_reply_off_by_default_and_not_for_actions(tmp_path):
    e = await menv(tmp_path, "llm_first")
    try:
        await e.chat("what is the temp sensor temperature?")
        assert e.llm.phrase_requests == []
    finally:
        await e.close()
    e = await menv(tmp_path, "llm_first", natural_read_replies=True)
    try:
        await e.chat("turn on the living room lamp")
        assert e.llm.phrase_requests == []
    finally:
        await e.close()


async def test_list_goes_to_the_llm_in_llm_first_and_is_phrased(tmp_path):
    e = await menv(tmp_path, "llm_first", natural_read_replies=True)
    try:
        e.llm.script.append((r"devices", {"calls": [{"tool": "list_devices", "args": {}}], "say": ""}))
        _, ev = await e.chat("devices")
        assert one(ev, "final")["route"] == "llm" and "Ceiling light" in one(ev, "final")["text"]
    finally:
        await e.close()


# ------------------------------------------------------------------------------------------------------ units
def test_numbers_and_polarity_helpers():
    assert numbers_ok("It is 24 degrees", {24.5}) and not numbers_ok("It is 31 degrees", {24.5})
    assert numbers_ok("50 percent", {50.0}) and not numbers_ok("51 percent", {50.0})
    assert polarity_conflict("The lamp is off", "Lamp: power on") and not polarity_conflict("The lamp is on", "Lamp: power on")
    assert polarity_conflict("Door unlocked", "Door locked") and not polarity_conflict("Lock it", "Door locked")
    assert phrase_verdict("", "x") == "empty" and phrase_verdict("a" * 300, "x") == "too_long"
    assert demo_devices()
