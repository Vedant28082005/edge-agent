"""The scheduler end to end through the real Agent + fake hub: the fast path and the router execute through the SAME executor
as an LLM plan (budgets, policy, confirmation tokens, verify/settle, action log), guests, fall-through, traces, ablation."""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import GUEST_SCOPE, collect, make_env, names, one
from test_confirmation import approve_when_asked

from agent.util import action_hash


async def fast_env(tmp_path, **kw):
    return await make_env(tmp_path, sched=kw.pop("sched", {}), **kw)


def hub_sets(e):
    return [c for c in e.fake.calls if c["tool"] == "set_state"]


def log_rows(e, status=None):
    return [r for r in e.agent.store.action_log(500) if status is None or r["status"] == status]


def last_trace(e):
    return e.agent.store.traces(1)[0]["detail"]


# ---------------------------------------------------------------------------------------------------- fast path
async def test_fast_path_answers_without_the_llm_and_uses_the_same_executor(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx, ev = await e.chat("turn on the bedroom fan")
        assert one(ev, "final")["route"] == "fast" and e.llm.requests == []
        assert [n for n in names(ev) if n != "token"] == ["tool_call", "tool_result", "status", "final"]
        (c,) = hub_sets(e)
        assert c["args"] == {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}
        assert c["meta"]["user_id"] == "alice" and c["meta"]["call_id"]              # executor-attached, out of band
        assert e.fake.state["ep_a1000002"]["power"] is True
        row = next(r for r in log_rows(e) if r["tool"] == "set_state")
        assert row["status"] == "done" and row["ok"] == 1 and row["reason"].startswith("user request (fast path)")
        assert row["trigger_kind"] == "user"
        t = last_trace(e)
        assert (t["route"], t["source"], t["llm_calls"], t["cache_hit"], t["constrained"], t["outcome"]) == \
               ("fast", "fast_path", 0, None, False, "ok")
        assert t["fast_reason"] == "device_match" and t["candidates"][0][0] == "ep_a1000002"
        assert t["flags"] == {"fast_path": True, "router": True, "constrained_decoding": True, "prefix_cache": True}
        assert "turn on" not in json.dumps(t)                                         # no user text in a trace
    finally:
        await e.close()


async def test_fast_and_llm_paths_produce_the_same_hub_calls_and_action_log(tmp_path):
    outcomes = []
    for sched in ({}, {"fast_path": False, "router": False}):
        e = await fast_env(tmp_path, sched=sched)
        try:
            await e.chat("turn off the bedroom fan")
            (c,) = hub_sets(e)
            row = next(r for r in log_rows(e) if r["tool"] == "set_state")
            outcomes.append((c["args"], sorted(c["meta"]), row["status"], row["ok"], row["args"], row["detail"],
                             len(e.llm.requests)))
        finally:
            await e.close()
    assert outcomes[0][:-1] == outcomes[1][:-1]
    assert (outcomes[0][-1], outcomes[1][-1]) == (0, 1)                               # only the LLM path used the model


async def test_budgets_apply_to_the_fast_path(tmp_path):
    e = await fast_env(tmp_path, max_calls=1)
    try:
        ctx, ev = await e.chat("turn off all the lights")                             # two lights, one call allowed
        assert len(hub_sets(e)) == 1
        assert "ran out of budget" in one(ev, "final")["text"]
        assert last_trace(e)["outcome"] in ("partial", "failed")
    finally:
        await e.close()


async def test_group_command_runs_every_call_and_reports_once(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx, ev = await e.chat("turn off all the lights")
        assert sorted(c["args"]["endpoint_id"] for c in hub_sets(e)) == ["ep_a1000001", "ep_a1000006"]
        assert one(ev, "final")["text"].startswith("Turning off")
    finally:
        await e.close()


async def test_motion_is_verified_asynchronously_exactly_like_an_llm_call(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx, ev = await e.chat("open the blinds")
        assert one(ev, "final")["route"] == "fast"
        assert "still moving" in one(ev, "final")["text"]
        settled = one(ev, "action_settled")
        assert settled["endpoint_id"] == "ep_a1000005" and settled["call_id"] == one(ev, "tool_result")["call_id"]
        assert names(ev).index("final") < names(ev).index("action_settled")
    finally:
        await e.close()


# ---------------------------------------------------------------------------------------------------- risk: confirm
async def test_risky_fast_command_asks_the_user_and_attaches_the_token_out_of_band(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx = e.agent.new_request("alice", "s1", "unlock the front door")
        task = asyncio.create_task(collect(ctx))
        for _ in range(100):
            if e.agent.confirmations.pending_ids():
                break
            await asyncio.sleep(0.02)
        assert not hub_sets(e)                                                        # nothing reaches the hub before approval
        await approve_when_asked(e)
        ev = await task
        req = one(ev, "confirmation_request")
        assert req["action"] == {"type": "set_state", "endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"}
        assert req["action_hash"] == action_hash(req["action"])
        (c,) = hub_sets(e)
        tok = c["meta"]["confirmation_token"]
        assert "confirmation_token" not in c["args"]
        assert one(ev, "final")["route"] == "fast" and e.llm.requests == []
        blob = json.dumps(ev) + json.dumps(log_rows(e)) + json.dumps(e.agent.store.recent("alice", "s1", 20))
        assert tok not in blob                                                         # never in events, action log, history, trace
        assert last_trace(e)["outcome"] == "ok"
    finally:
        await e.close()


async def test_declined_fast_command_does_nothing(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx = e.agent.new_request("alice", "s1", "lock the front door")
        task = asyncio.create_task(collect(ctx))
        await approve_when_asked(e, approve=False)
        ev = await task
        assert not hub_sets(e) and "won't do that" in one(ev, "final")["text"]
        assert last_trace(e)["outcome"] == "needs_confirmation"
    finally:
        await e.close()


async def test_agent_recomputes_the_hash_for_a_fast_path_confirmation(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx = e.agent.new_request("alice", "s1", "unlock the front door")
        task = asyncio.create_task(collect(ctx))
        for _ in range(100):
            if e.agent.confirmations.pending_ids():
                break
            await asyncio.sleep(0.02)
        rid = e.agent.confirmations.pending_ids()[0]
        action = e.agent.confirmations.pending_action(rid)
        tok = e.fake.mint("alice", action)
        bad = {"decision": "approved", "user_id": "alice", "role": "member", "action": action, "action_hash": "0" * 64,
               "confirmation_token": tok}
        status, body = await e.agent.handle_decision(rid, bad)
        assert status == 409 and body["error"] == "action_hash_mismatch" and not hub_sets(e)
        good = {**bad, "action_hash": action_hash(action)}
        assert (await e.agent.handle_decision(rid, good))[0] == 202
        await task
        assert len(hub_sets(e)) == 1
    finally:
        await e.close()


async def test_guest_gets_a_denial_and_nothing_runs_or_prompts(tmp_path):
    e = await fast_env(tmp_path)
    try:
        for text in ("turn on the bedroom fan", "unlock the front door"):
            ctx, ev = await e.chat(text, "gina", "s1", role="guest", scope=GUEST_SCOPE)
            f = one(ev, "final")
            assert f["route"] == "fast" and "Guests can't control devices" in f["text"]
            assert "confirmation_request" not in names(ev)
        assert not hub_sets(e) and e.llm.requests == [] and not e.agent.confirmations.pending_ids()
        assert [r["status"] for r in log_rows(e, "denied_guest")] == ["denied_guest"] * 2
        assert last_trace(e)["outcome"] == "denied"
    finally:
        await e.close()


# ---------------------------------------------------------------------------------------------------- fall-through
@pytest.mark.parametrize("text", ["turn on the garage lamp and fan", "what is the airspeed of a swallow", "make it brighter"])
async def test_unresolved_input_goes_to_the_llm(tmp_path, text):
    e = await fast_env(tmp_path)
    try:
        ctx, ev = await e.chat(text)
        assert one(ev, "final")["route"] == "llm" and len(e.llm.requests) == 1
        t = last_trace(e)
        assert t["route"] == "llm" and t["llm_calls"] == 1 and t["constrained"] is True and t["cache_hit"] is False
        assert t["fast_reason"] and t["prompt_tokens"] > 0
    finally:
        await e.close()


# ---------------------------------------------------------------------------------------------------- router
async def test_sensor_question_is_answered_by_read_sensor_without_the_llm(tmp_path):
    e = await fast_env(tmp_path)
    try:
        ctx, ev = await e.chat("what is the temperature in the bedroom?")
        f = one(ev, "final")
        assert f["route"] == "sensor" and "24.5" in f["text"] and "Bedroom" in f["text"] and e.llm.requests == []
        assert [c["args"] for c in e.fake.calls if c["tool"] == "read_sensor"] == [{"endpoint_id": "ep_a1000003", "capability": "temperature"}]
        t = last_trace(e)
        assert (t["route"], t["source"]) == ("sensor", "sensor")
        assert [c["source"] for c in t["candidates"]] == ["sensor", "llm"] and t["candidates"][0]["flag"] == "est"
        costs = e.agent.sched.stats()["costs"]["sensor"]
        assert costs["n"] == 1 and costs["flag"] == "measured"                         # EWMA fed by the observed latency
    finally:
        await e.close()


async def test_failed_sensor_falls_up_the_ladder_to_the_llm_and_trips_the_breaker(tmp_path):
    e = await fast_env(tmp_path)
    try:
        orig = e.fake.tool

        def broken(name, a, meta):
            if name == "read_sensor":
                e.fake.calls.append({"tool": name, "args": dict(a), "meta": dict(meta)})
                return {"ok": False, "error": "offline"}
            return orig(name, a, meta)

        e.fake.tool = broken
        ctx, ev = await e.chat("what is the temperature in the bedroom?")
        t = last_trace(e)
        assert t["route"] == "llm" and t["fallbacks"] == ["sensor"] and one(ev, "final")["route"] == "llm"
        assert len(e.llm.requests) >= 1
        ctx, ev = await e.chat("what is the temperature in the bedroom?")
        t2 = last_trace(e)
        assert t2["fallbacks"] == [] and next(c for c in t2["candidates"] if c["source"] == "sensor")["reason"] == "recent_failure"
    finally:
        await e.close()


async def test_image_only_turn_defaults_to_a_describe_question_via_vision(tmp_path):
    from agent.sched.router import BackendAnswer

    class Vlm:
        def supports(self, s):
            return s == "vlm_short"

        async def answer(self, s, q, images):
            assert q == "What do you see?"           # the default describe question, not empty text
            return BackendAnswer(True, 'The camera view (unverified description): "a tidy kitchen."', 80.0)

    e = await fast_env(tmp_path)
    try:
        e.agent.sched.router.avail.register(Vlm())
        ctx = e.agent.new_request("alice", "s1", "", "member", images=1)        # image only, no text
        ev = await collect(ctx)
        final = one(ev, "final")
        assert final["route"] == "vlm" and "tidy kitchen" in final["text"].lower()
        assert "can't analyse images" not in final["text"].lower()
        assert e.llm.requests == []
    finally:
        await e.close()


async def test_image_only_turn_falls_back_when_no_vision_node_is_enabled(tmp_path):
    e = await make_env(tmp_path)                       # default sched: fast_path=False, router=False, no backends
    try:
        ctx = e.agent.new_request("alice", "s1", "", "member", images=1)
        ev = await collect(ctx)
        assert one(ev, "final")["text"] == "I can't analyse images yet."
        assert e.llm.requests == []
    finally:
        await e.close()


async def test_a_registered_vision_backend_is_used_for_presence_and_costed(tmp_path):
    from agent.sched.router import BackendAnswer

    class Yolo:
        def supports(self, s):
            return s == "yolo_pi"

        async def answer(self, s, q, images):
            return BackendAnswer(True, "Yes, one person.", 120.0)

    e = await fast_env(tmp_path)
    try:
        e.agent.sched.router.avail.register(Yolo())
        # images are validated and dropped at the API layer, so only their COUNT reaches the turn (contract gap)
        ctx = e.agent.new_request("alice", "s1", "is there a person at the door?", images=1)
        ev = await collect(ctx)
        assert one(ev, "final")["route"] == "yolo" and "one person" in one(ev, "final")["text"].lower()
        t = last_trace(e)
        assert t["route"] == "yolo" and t["source"] == "yolo_pi" and e.llm.requests == []
        assert e.agent.sched.stats()["costs"]["yolo_pi"]["ms"] == 120.0
    finally:
        await e.close()


# ---------------------------------------------------------------------------------------------------- ablation / trace
async def test_each_flag_can_be_ablated_independently(tmp_path):
    e = await fast_env(tmp_path)
    try:
        e.agent.set_flags(fast_path=False)
        ctx, ev = await e.chat("turn on the bedroom fan")
        assert one(ev, "final")["route"] == "llm" and len(e.llm.requests) == 1
        assert last_trace(e)["flags"] == {"fast_path": False, "router": True, "constrained_decoding": True, "prefix_cache": True}
        ctx, ev = await e.chat("what is the temperature in the bedroom?")             # router still on
        assert last_trace(e)["route"] == "sensor"
        e.agent.set_flags(router=False)
        ctx, ev = await e.chat("what is the temperature in the bedroom?")
        assert last_trace(e)["route"] == "llm"
        e.agent.set_flags(constrained_decoding=False, prefix_cache=False)
        assert e.agent.llm.constrained is False and e.agent.llm.prefix_cache is False
        await e.chat("hello")
        t = last_trace(e)
        assert t["constrained"] is False and t["flags"]["prefix_cache"] is False
        e.agent.set_flags(constrained=True, fast_path=True, router=True, prefix_cache=True)  # v1 name still works
        assert e.agent.llm.constrained is True
        with pytest.raises(ValueError):
            e.agent.set_flags(turbo=True)
    finally:
        await e.close()


async def test_runtime_endpoint_switches_the_flags_and_rejects_junk(tmp_path):
    import httpx
    from agent.app import create_app
    from agent.auth import ApiAuth
    e = await fast_env(tmp_path)
    try:
        app = create_app(e.agent, ApiAuth.from_secret("gw-secret-for-tests-0123456789", str(tmp_path / "v.sha256")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent",
                                     headers={"X-Service-Secret": "gw-secret-for-tests-0123456789"}) as c:
            r = await c.post("/v1/runtime", json={"fast_path": False, "prefix_cache": False})
            assert r.status_code == 200 and r.json() == {"constrained": True, "fast_path": False, "router": True,
                                                         "constrained_decoding": True, "prefix_cache": False}
            assert (await c.post("/v1/runtime", json={"turbo": True})).status_code == 400
            assert (await c.post("/v1/runtime", json={"fast_path": "yes"})).status_code == 400
            st = (await c.get("/v1/stats")).json()
            assert st["sched"]["flags"]["fast_path"] is False and "routes" in st["sched"] and "costs" in st["sched"]
    finally:
        await e.close()


async def test_one_trace_per_turn_on_the_bus_and_in_the_action_log(tmp_path):
    e = await fast_env(tmp_path)
    seen = []

    async def on_trace(ev):
        seen.append(ev)

    e.agent.bus.subscribe("sched.trace", on_trace, "test")
    try:
        await e.chat("turn on the bedroom fan")
        await e.chat("hello there")
        await asyncio.sleep(0.1)
        assert len(e.agent.store.traces(50)) == 2 and not log_rows(e, "trace") and len(seen) == 2
        assert [s.data["route"] for s in seen] == ["fast", "llm"]
        need = {"req_id", "route", "source", "candidates", "latency_ms", "prompt_tokens", "cached_tokens", "generated_tokens",
                "cache_hit", "constrained", "outcome", "flags", "fast_reason", "confidence", "llm_calls"}
        assert need <= set(seen[0].data)
        assert seen[0].data["llm_raw"] == ""                            # (S8f) the fast path never calls the LLM
        assert seen[1].data["llm_raw"]                                  # the LLM turn's raw reply is captured, truncated
        stats = e.agent.sched.stats()["routes"]
        assert stats["fast"]["n"] == 1 and stats["llm"]["n"] == 1
    finally:
        await e.close()


async def test_error_turns_still_emit_a_trace(tmp_path):
    e = await fast_env(tmp_path, max_steps=0, sched={"fast_path": False, "router": False})
    try:
        ctx, ev = await e.chat("hello")
        assert one(ev, "error")["code"] == "budget_exceeded"
        assert last_trace(e)["outcome"] == "error:budget_exceeded"
    finally:
        await e.close()


async def test_scenes_from_list_devices_enable_the_scene_fast_path(tmp_path):
    e = await fast_env(tmp_path)
    try:
        assert e.agent.registry.scene_names() == ("Good night",) and e.agent.registry.list_version is not None
        ctx, ev = await e.chat("good night")
        assert one(ev, "final")["route"] == "fast" and e.llm.requests == []
        runs = [c for c in e.fake.calls if c["tool"] == "run_scene"]
        assert len(runs) == 1 and runs[0]["args"] == {"scene": "Good night"}
    finally:
        await e.close()
