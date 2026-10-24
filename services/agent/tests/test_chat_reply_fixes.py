"""Three reply-composition bugs in chat.py:

1. A bare error_input (homebench) must produce one clear reply, never "Couldn't do this part: could not do this
   part." stacked onto the generic no-plan text.
2. When an action already ran this turn (fast path, ambient fallback, or the homebench calls themselves), a
   parse failure elsewhere in the same reply must not append an error line on top of that result.
3. A clarifying question's short "yes"-shaped follow-up must resolve using the ORIGINAL ambient intent (domain +
   direction), never be re-derived from the follow-up text itself."""
from __future__ import annotations

from conftest import one

from agent.chat import apply_hb_failures, no_plan_text
from agent.devices import DeviceRegistry
from agent.executor import CallOutcome
from agent.mock import demo_devices

from test_ambient_polarity import ONE_LIGHT, _bright_plan, custom_env


def reg():
    r = DeviceRegistry()
    r.replace(demo_devices(), 1)
    return r


def ok_outcome() -> CallOutcome:
    return CallOutcome("c1", "set_state", {"endpoint_id": "ep_a1000001"}, {}, True, "done")


# --------------------------------------------------------------------------------------------- apply_hb_failures
def test_bare_error_input_is_one_clear_reply():
    r = reg()
    final = apply_hb_failures("I couldn't work out what to do.", ["could not do this part"], [], r)
    assert final == no_plan_text(r)
    assert final.count("couldn't") + final.count("Couldn't") <= 1


def test_multiple_bare_error_inputs_still_one_clear_reply():
    r = reg()
    final = apply_hb_failures("I couldn't work out what to do.",
                              ["could not do this part", "could not do this part"], [], r)
    assert final == no_plan_text(r)


def test_no_error_line_appended_when_an_action_already_ran():
    r = reg()
    final = apply_hb_failures("Ceiling light is on.", ["could not do this part"], [ok_outcome()], r)
    assert final == "Ceiling light is on."


def test_informative_failure_still_appended_when_nothing_ran():
    r = reg()
    final = apply_hb_failures("I couldn't work out what to do.", ["unknown method for 'x.y.z()'"], [], r)
    assert "unknown method for 'x.y.z()'" in final
    assert final != no_plan_text(r)


def test_no_failures_leaves_reply_untouched():
    r = reg()
    assert apply_hb_failures("Done.", [], [], r) == "Done."


# --------------------------------------------------------------------------------------------- pending confirmation
async def test_clarifying_question_then_yes_applies_original_direction(tmp_path):
    """Bug: a "yes" after "it's way too bright here" (never corrected, so it falls back to a clarifying question
    about the one real candidate) must still dim/turn off that light -- never re-interpret "yes" on its own,
    which carries no direction and could default to turning it on."""
    e = await custom_env(tmp_path, ONE_LIGHT)
    try:
        e.llm.script.append((r"bright here", _bright_plan(True)))        # always wrong: turns the light ON
        ctx, ev = await e.chat("it's way too bright here")
        final = one(ev, "final")
        assert "?" in final["text"]
        assert not [c for c in e.fake.calls if c["tool"] == "set_state"]
        requests_before = len(e.llm.requests)

        ctx2, ev2 = await e.chat("yes")
        final2 = one(ev2, "final")
        sets = [c for c in e.fake.calls if c["tool"] == "set_state"]
        assert len(sets) == 1 and sets[0]["args"]["value"] is False
        assert len(e.llm.requests) == requests_before      # resolved without asking the model again
        assert final2["route"] == "fast"
    finally:
        await e.close()


async def test_unrelated_reply_after_clarifying_question_does_not_reuse_pending(tmp_path):
    """A reply that is not a short confirmation abandons the pending question rather than letting some later
    unrelated "yes" misfire on stale intent."""
    e = await custom_env(tmp_path, ONE_LIGHT)
    try:
        e.llm.script.append((r"bright here", _bright_plan(True)))
        e.llm.script.append((r"status", {"calls": [], "say": "It is on."}))
        await e.chat("it's way too bright here")
        await e.chat("what is the status")
        ctx3, ev3 = await e.chat("yes")
        sets = [c for c in e.fake.calls if c["tool"] == "set_state"]
        assert not sets          # the pending clarification was discarded, not silently resolved later
    finally:
        await e.close()
