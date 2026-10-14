"""Per-user conversation window (token budget, rolling summary, strict isolation) and the byte-stable cached prefix."""
from __future__ import annotations

import json

import pytest
from conftest import make_env, names, one

from agent.sched.memory import MemoryWindow
from agent.sched.prefix import PrefixTracker, cache_fields
from agent.store import Store
from agent.util import est_tokens


def msgs(store, user, session, n, tag, guest=False):
    for i in range(n):
        store.add_message(user, session, "user", f"{tag} question number {i} " + "x" * 40, guest)
        store.add_message(user, session, "assistant", f"{tag} answer number {i} " + "y" * 40, guest)


def traces(env):
    return [r["detail"] for r in reversed(env.agent.store.traces(500))]


# ---------------------------------------------------------------------------------------------------- window
def test_window_is_chosen_by_token_budget_and_opens_with_a_user_turn():
    st = Store()
    msgs(st, "alice", "s1", 12, "ALICE")
    mw = MemoryWindow(st, window_tokens=100, summary_tokens=40, max_messages=50)
    w = mw.load("alice", "s1")
    assert 0 < w.tokens <= 100 and len(w.history) < 12 and w.history[0][0] == "user"
    assert sum(est_tokens(t) for _, t in w.history) == w.tokens
    assert w.history[-1][1].startswith("ALICE answer number 11")             # newest content wins the budget
    tiny = MemoryWindow(st, window_tokens=10, summary_tokens=40).load("alice", "s1")
    assert tiny.history == []                                                 # nothing fits: no half messages


def test_overflow_is_folded_into_a_bounded_rolling_summary_newest_kept():
    st = Store()
    mw = MemoryWindow(st, window_tokens=100, summary_tokens=30)
    for i in range(10):
        msgs(st, "alice", "s1", 1, f"T{i:02d}")
        mw.fold("alice", "s1")
    s = st.summary("alice", "s1")
    assert s and est_tokens(s) <= 30 + 1
    assert "T09" in s or "T08" in s or "T07" in s                            # recent past survives
    assert "T00" not in s                                                    # oldest rolled off
    assert mw.fold("alice", "s1") is False                                   # nothing new to fold
    w = mw.load("alice", "s1")
    assert est_tokens(w.summary) <= 30
    assert not any("T00" in t for _, t in w.history)


def test_strict_per_user_isolation_even_with_the_same_session_id():
    st = Store()
    mw = MemoryWindow(st, window_tokens=100, summary_tokens=40)
    msgs(st, "alice", "s1", 8, "ALICE-SECRET")
    msgs(st, "bob", "s1", 8, "BOB")
    for u in ("alice", "bob"):
        mw.fold(u, "s1")
    a, b = mw.load("alice", "s1"), mw.load("bob", "s1")
    blob_a = json.dumps([a.history, a.summary])
    blob_b = json.dumps([b.history, b.summary])
    assert "BOB" not in blob_a and "ALICE" not in blob_b
    assert "ALICE-SECRET" in blob_a and "BOB" in blob_b
    assert mw.load("carol", "s1").history == [] and mw.load("carol", "s1").summary == ""
    assert mw.load("alice", "other-session").history == []                    # sessions are separate too
    st.forget_user("alice")
    assert mw.load("alice", "s1").history == [] and "BOB" in json.dumps(mw.load("bob", "s1").history)


def test_guests_have_no_persistent_window_or_summary():
    st = Store()
    mw = MemoryWindow(st, window_tokens=100, summary_tokens=40)
    msgs(st, "guest1", "s1", 6, "GUEST", guest=True)
    assert mw.load("guest1", "s1", guest=True).history                        # in-process only
    assert mw.fold("guest1", "s1", guest=True) is False
    assert st.summary("guest1", "s1") == ""
    assert mw.load("guest1", "s1", guest=False).history == []                 # nothing in SQLite
    assert st._q("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 0


def test_custom_summariser_hook_is_used():
    st = Store()
    mw = MemoryWindow(st, window_tokens=60, summary_tokens=40, summarizer=lambda prev, rows: f"{len(rows)} older turns")
    msgs(st, "alice", "s1", 6, "A")
    assert mw.fold("alice", "s1") and st.summary("alice", "s1").endswith("older turns")


# ---------------------------------------------------------------------------------------------------- prefix
def test_prefix_tracker_semantics():
    t = PrefixTracker()
    assert t.observe("aa", {"prompt_n": 100, "cache_n": 0}).cache_hit is False       # first request: nothing to reuse
    assert t.observe("aa", {"prompt_n": 100, "cache_n": 90}).cache_hit is True
    assert t.observe("aa", {"prompt_n": 100, "cache_n": 0}).cache_hit is False       # same bytes but the server missed
    assert t.observe("aa", {}).cache_hit is True                                      # server reports nothing: trust the bytes
    assert t.observe("bb", {"cache_n": 50}).cache_hit is False                       # prefix changed
    assert t.stats() == {"requests": 5, "prefix_stable": 3, "cache_hits": 2}
    assert cache_fields(True, 3) == {"cache_prompt": True, "id_slot": 3}
    assert cache_fields(False, 3) == {"cache_prompt": False}


async def test_prefix_bytes_are_identical_across_turns_users_and_histories(tmp_path):
    e = await make_env(tmp_path)                                                       # LLM path (sched off)
    try:
        v0 = e.agent.builder.prefix_version
        for user, sess, text in (("alice", "s1", "what time is it"), ("bob", "s9", "play something"),
                                 ("alice", "s1", "and now?"), ("alice", "s1", "thanks " * 40), ("bob", "s9", "again")):
            await e.chat(text, user, sess)
        systems = [r[0]["content"] for r in e.llm.requests]
        assert len(systems) == 5 and len(set(systems)) == 1                            # byte-identical static prefix
        assert systems[0] == e.agent.builder.prefix
        tr = traces(e)
        assert len({t["prefix_hash"] for t in tr}) == 1 and tr[0]["prefix_hash"] == e.agent.builder.prefix_hash
        assert [t["cache_hit"] for t in tr] == [False, True, True, True, True]
        assert e.agent.builder.prefix_version == v0                                     # never rebuilt without a registry change
        # nothing per-user or per-turn leaked into the prefix; it all sits after it
        for r in e.llm.requests:
            assert r[0]["role"] == "system" and "bob" not in r[0]["content"] and "thanks" not in r[0]["content"]
            assert r[-1]["role"] == "user"
    finally:
        await e.close()


async def test_registry_change_changes_the_prefix_once_and_costs_one_miss(tmp_path):
    import asyncio
    e = await make_env(tmp_path)
    try:
        await e.chat("hello", "alice")
        await e.chat("hello again", "alice")
        before = e.agent.builder.prefix_hash
        e.fake.rename("ep_a1000002", "Ventilator")
        for _ in range(100):
            if e.agent.builder.prefix_hash != before:
                break
            await asyncio.sleep(0.02)
        await e.chat("and now", "alice")
        await e.chat("and again", "alice")
        tr = traces(e)
        assert [t["prefix_hash"] == before for t in tr] == [True, True, False, False]
        assert [t["cache_hit"] for t in tr] == [False, True, False, True]
    finally:
        await e.close()


async def test_memory_never_crosses_users_in_the_real_prompt(tmp_path):
    e = await make_env(tmp_path)
    try:
        await e.chat("my pin hint is PURPLE-ELEPHANT", "alice", "s1")
        for i in range(8):
            await e.chat(f"filler message {i} " + "z" * 60, "alice", "s1")
        await e.chat("what did I say earlier?", "bob", "s1")                           # same session id, other user
        bob_prompt = json.dumps(e.llm.requests[-1])
        assert "PURPLE-ELEPHANT" not in bob_prompt and "filler" not in bob_prompt and "Earlier:" not in bob_prompt
        await e.chat("what did I say earlier?", "alice", "s1")
        alice_prompt = e.llm.requests[-1]
        assert any("filler" in m["content"] for m in alice_prompt[1:])                 # her own window is there
        assert alice_prompt[0] == e.llm.requests[-2][0]                                # and the prefix is untouched
        assert alice_prompt[-1]["content"].startswith("Earlier: ")                      # rolling summary, in the tail
    finally:
        await e.close()


async def test_guest_conversation_is_never_persisted_or_summarised(tmp_path):
    e = await make_env(tmp_path)
    try:
        for i in range(6):
            await e.chat(f"guest message {i}", "gina", "s1", role="guest")
        st = e.agent.store
        assert st._q("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 0 and st.summary("gina", "s1") == ""
    finally:
        await e.close()
