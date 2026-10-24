"""Tests for the homebench LLM format (agent/homebench.py): registry-driven table, prompt text, and reply parsing.
Covers: a prompt snapshot (byte-stable for a fixed registry+states), parser edge cases (tuples, trailing comma,
arg:value, braces, error_input), single/multi/ambiguous/error cases, and that the default "json" format (used by
every other test in this suite) is completely unaffected."""
from __future__ import annotations

import pytest

from conftest import make_env, one

from agent import homebench
from agent.config import AgentConfig, load_config
from agent.devices import DeviceRegistry
from agent.mock import demo_devices
from agent.planner import Plan, Planner, parse_homebench_plan
from agent.prompt import PromptBuilder


def reg_without_lock():
    """demo_devices() minus the risk:confirm front door -- kept out of this module's prompt entirely regardless,
    dropping it here just keeps assertions about `<device_method>` free of a device that can never appear in it."""
    reg = DeviceRegistry()
    reg.replace(demo_devices(), 1)
    return reg


def table():
    return homebench.build_table(reg_without_lock())


# --------------------------------------------------------------------------------------------- config
def test_llm_format_defaults_to_json():
    assert AgentConfig().llm_format == "json"


def test_llm_format_rejects_unknown_value(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"llm_format": "weird"}')
    with pytest.raises(ValueError):
        load_config(str(p))


# --------------------------------------------------------------------------------------------- builder table
def test_risk_confirm_endpoint_excluded_from_table():
    t = table()
    assert all(e.room != "hall" for e in t.entries)
    assert not any(ep == "ep_a1000004" for ep in t.labels)


def test_power_splits_into_two_zero_arg_methods():
    t = table()
    lamp_room, lamp_dev = t.labels["ep_a1000006"]
    assert (lamp_room, lamp_dev, "turn_on") in t.by_name
    assert (lamp_room, lamp_dev, "turn_off") in t.by_name
    assert t.by_name[(lamp_room, lamp_dev, "turn_on")].fixed_value is True
    assert t.by_name[(lamp_room, lamp_dev, "turn_off")].fixed_value is False


def test_mapped_capability_gets_its_method():
    t = table()
    room, dev = t.labels["ep_a1000005"]        # Blinds, position -> set_degree
    assert (room, dev, "set_degree") in t.by_name
    entry = t.by_name[(room, dev, "set_degree")]
    assert entry.endpoint_id == "ep_a1000005" and entry.capability == "position" and entry.arg_type == "int"


def test_unmapped_sensor_capability_gets_no_method():
    t = table()
    room, dev = t.labels["ep_a1000003"]        # Temp sensor: no actuator caps at all
    assert not any(e for e in t.entries if e.room == room and e.device == dev)


def test_rooms_and_devices_are_snake_case():
    t = table()
    assert t.labels["ep_a1000005"][0] == "living_room"
    assert all(" " not in f"{r}{d}" for r, d in t.labels.values())


# --------------------------------------------------------------------------------------------- prompt text / snapshot
def test_prompt_is_one_system_message_no_grammar():
    pb = PromptBuilder(reg_without_lock(), format="homebench")
    prompt = pb.build("turn on the lamp")
    assert len(prompt.messages) == 1
    assert prompt.messages[0]["role"] == "system"


def test_header_text_is_exact():
    pb = PromptBuilder(reg_without_lock(), format="homebench")
    assert pb.prefix.startswith(homebench.HEADER)


def test_trailer_is_exact_and_last():
    pb = PromptBuilder(reg_without_lock(), format="homebench")
    content = pb.build("turn on the lamp").messages[0]["content"]
    assert content.endswith(homebench.build_trailer("turn on the lamp"))
    assert "<Machine instructions:>" in content and content.rstrip().endswith("<Machine instructions:>")


def test_prefix_byte_stable_for_a_fixed_registry_and_states():
    reg = reg_without_lock()
    states = {"ep_a1000006": {"power": True}}
    t1, table1 = homebench.build_prefix(reg, states)
    t2, table2 = homebench.build_prefix(reg, states)
    assert t1 == t2
    assert table1.by_name.keys() == table2.by_name.keys()


def test_prefix_changes_when_states_change():
    """Documents the cache trade-off: a live-state change changes the prefix bytes (rebuild+warm required)."""
    reg = reg_without_lock()
    a, _ = homebench.build_prefix(reg, {"ep_a1000006": {"power": True}})
    b, _ = homebench.build_prefix(reg, {"ep_a1000006": {"power": False}})
    assert a != b


def test_home_state_shows_range_and_options():
    reg = reg_without_lock()
    text, t = homebench.build_prefix(reg, {"ep_a1000005": {"position": 40}})
    assert "position: 40 (range: 0 - 100)" in text


def test_device_method_block_concatenates_entries():
    reg = reg_without_lock()
    text, _ = homebench.build_prefix(reg)
    dm = text.split("<device_method>\n")[1]
    assert "living_room.cover.set_degree(degree:int);" in dm
    assert dm.count(";") == dm.count("(")        # every call is `name(args);`, none dropped


# --------------------------------------------------------------------------------------------- parser edge cases
def test_parse_single_call():
    t = table()
    res = homebench.parse_reply("living_room.light.turn_off()", t)
    assert res.calls == [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000006", "capability": "power", "value": False}}]
    assert not res.failures


def test_parse_multi_call_and_trailing_comma():
    t = table()
    res = homebench.parse_reply("living_room.cover.set_degree(40),bedroom.fan.turn_off(),", t)
    assert len(res.calls) == 2
    assert res.calls[0]["args"] == {"endpoint_id": "ep_a1000005", "capability": "position", "value": 40}
    assert res.calls[1]["args"] == {"endpoint_id": "ep_a1000002", "capability": "power", "value": False}


def test_parse_accepts_named_arg_and_braces():
    t = table()
    res = homebench.parse_reply("{living_room.cover.set_degree(degree:40)}", t)
    assert res.calls[0]["args"]["value"] == 40


def test_parse_tuple_argument_does_not_split_on_inner_comma():
    """`_split_top_level` must split only at paren depth 0 -- a tuple-shaped argument is one piece, not two."""
    t = table()
    parts = homebench._split_top_level("living_room.cover.set_degree(40),error_input")
    assert parts == ["living_room.cover.set_degree(40)", "error_input"]


def test_parse_error_input_is_a_failure_not_a_crash():
    t = table()
    res = homebench.parse_reply("error_input,bedroom.light.set_brightness(40)", t)
    assert res.failures == ["could not do this part"]
    assert len(res.calls) == 1


def test_parse_unknown_method_never_guessed():
    t = table()
    res = homebench.parse_reply("garage.light.turn_on()", t)
    assert not res.calls and res.failures


def test_parse_out_of_range_value_rejected():
    t = table()
    res = homebench.parse_reply("bedroom.light.set_brightness(999)", t)
    assert not res.calls and res.failures


def test_parse_enum_option_validated():
    t = table()
    good = homebench.parse_reply("bedroom.light.set_brightness(0)", t)      # 0 is in-range, still accepted
    assert good.calls


def test_parse_empty_and_whitespace_only_reply():
    t = table()
    res = homebench.parse_reply("", t)
    assert res.calls == [] and res.failures == []


# --------------------------------------------------------------------------------------------- planner integration
def test_parse_homebench_plan_carries_failures_through():
    t = table()
    plan = parse_homebench_plan("error_input,living_room.light.turn_on()", t)
    assert isinstance(plan, Plan)
    assert plan.parsed_ok and len(plan.calls) == 1
    assert plan.failures == ["could not do this part"]


def test_render_calls_round_trips_power():
    t = table()
    calls = [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000006", "capability": "power", "value": True}}]
    assert homebench.render_calls(calls, t) == "living_room.light.turn_on(),"


# --------------------------------------------------------------------------------------------- end-to-end (S8l merge)
async def test_homebench_question_still_stops_after_one_read(tmp_path):
    """(S8l + S8k) The homebench-tuned model has no way to express a read at all (device_method only ever lists
    set_* methods), so a status question must be answered without it -- by the resolver's status fast path, same
    as the json format. This is the "stops after one read" guarantee for homebench: the fast path (format-agnostic)
    answers from a single get_state and the LLM is never even asked."""
    e = await make_env(tmp_path, sched={"fast_path": True, "router": False}, llm_format="homebench")
    try:
        e.fake.state["ep_a1000001"]["power"] = True
        e.fake.state["ep_a1000001"]["brightness"] = 42
        ctx, ev = await e.chat("what is the ceiling light status?")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        gets = [c for c in e.fake.calls if c["tool"] == "get_state"]
        assert len(gets) == 1
        assert "Ceiling light" in final["text"] and "42" in final["text"]
        assert not [c for c in e.fake.calls if c["tool"] == "set_state"]
    finally:
        await e.close()


async def test_homebench_question_with_no_fast_path_match_falls_back_without_calling_the_llm(tmp_path):
    """Fast path off, no other read mechanism available for homebench's state questions: the turn must still
    terminate (never loop or hang) and must never send the question to the LLM (it has no way to reply with a
    read at all, see module docstring)."""
    e = await make_env(tmp_path, sched={"fast_path": False, "router": False}, llm_format="homebench")
    try:
        ctx, ev = await e.chat("what is the ceiling light status?")
        final = one(ev, "final")
        assert e.llm.requests == []
        assert final["text"]
    finally:
        await e.close()
