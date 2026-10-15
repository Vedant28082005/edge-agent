"""agent 1.3.0: per-request scope (`allowed_rooms` / `allowed_scenes`), registry_version, scenes from list_devices, route enum.
Contract: contracts/openapi/agent-internal.yaml 1.3.0, device-hub-internal.yaml 1.4.0, mcp-tools.md (list_devices)."""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import GUEST_SCOPE, collect, make_env, names, one
from test_executor import script, setstate

from agent.devices import DeviceRegistry
from agent.schema import schema_endpoint_ids
from agent.scope import NOTHING, UNRESTRICTED, Scope, make_scope
from agent.util import action_hash

FAN, LIGHT, SENSOR, LOCK, BLINDS = "ep_a1000002", "ep_a1000001", "ep_a1000003", "ep_a1000004", "ep_a1000005"
BEDROOM = Scope(frozenset({"Bedroom"}), frozenset())
BEDROOM_SCENE = Scope(frozenset({"Bedroom"}), frozenset({"sc_9a8b7c6d"}))
LOCK_ACTION = {"type": "set_state", "endpoint_id": LOCK, "capability": "lock", "value": "unlocked"}


def hub_calls(env, *tools):
    return [c for c in env.fake.calls if c["tool"] in tools]


def once(env, calls):
    """A scripted plan that is answered once; the follow-up round after read tools gets an empty plan."""
    env.llm.script.clear()
    box = [{"calls": calls, "say": "ok"}]
    env.llm.script.append((".", lambda messages: box.pop() if box else {"calls": [], "say": "done"}))


def results(ev):
    return [d for n, d in ev if n == "tool_result"]


# ------------------------------------------------------------------------------------------------ parsing (fail closed)
def test_missing_means_nothing_for_member_and_guest():
    for role in ("member", "guest"):
        sc = make_scope(role)
        assert sc.rooms == frozenset() and sc.scenes == frozenset() and not sc.room_ok("Bedroom") and not sc.scene_id_ok("sc_1")
        sc = make_scope(role, ["Bedroom"])                                   # one field present, the other missing
        assert sc.rooms == {"Bedroom"} and sc.scenes == frozenset()


def test_null_is_unrestricted_only_for_admin_and_member_never_guest():
    assert make_scope("admin", None, None) == UNRESTRICTED and make_scope("member", None, None) == UNRESTRICTED
    assert make_scope("admin") == UNRESTRICTED                               # an admin with no field is not narrowed
    g = make_scope("guest", None, None)
    assert g.rooms == frozenset() and g.scenes == frozenset() and g.restricted
    g = make_scope("guest", ["Hall"], None)
    assert g.rooms == {"Hall"} and g.scenes == frozenset()                   # the null half of a guest scope is [] too


def test_malformed_values_fail_closed():
    for bad in ("Bedroom", 5, {"a": 1}, True):
        sc = make_scope("member", bad, bad)
        assert sc.rooms == frozenset() and sc.scenes == frozenset()
    assert make_scope("member", ["Bedroom", 3, "", None], []).rooms == {"Bedroom"}
    assert make_scope("root", None, None) == NOTHING                          # unknown role


def test_room_labels_compare_like_the_registry():
    """Registry labels were reduced to [\\w .'&-] at ingest; the gateway's labels get the same treatment."""
    assert make_scope("member", ["Kids <room>"], []).room_ok("Kids room")


# ------------------------------------------------------------------------------------------------ executor enforcement
async def test_out_of_scope_set_state_never_reaches_the_hub_on_the_llm_path(env):
    env.agent.llm.constrained = False
    script(env, [setstate(LOCK, "lock", "unlocked"), setstate(FAN, "power", True)])
    ctx, ev = await env.chat("do both", scope=BEDROOM)
    res = results(ev)
    assert sorted(str(r["result"].get("error")) for r in res) == ["None", "out_of_scope"]      # risky calls are ordered last
    assert [c["args"]["endpoint_id"] for c in hub_calls(env, "set_state")] == [FAN]       # only the in-scope one went out
    assert "confirmation_request" not in names(ev) and not env.agent.confirmations.pending_ids()
    rows = [r for r in env.agent.store.action_log(50) if r["status"] == "out_of_scope"]
    assert len(rows) == 1 and rows[0]["tool"] == "set_state" and rows[0]["ok"] == 0
    assert env.agent.metrics.calls_rejected.get("out_of_scope") == 1


async def test_out_of_scope_reads_get_state_read_sensor_list_devices(env):
    env.agent.llm.constrained = False
    s = {"tool": "read_sensor", "args": {"endpoint_id": SENSOR, "capability": "temperature"}}
    once(env, [s, {"tool": "get_state", "args": {"endpoint_id": LOCK}}, {"tool": "get_state", "args": {"room": "Hall"}},
                 {"tool": "list_devices", "args": {"room": "Hall"}}])
    ctx, ev = await env.chat("look around", scope=Scope(frozenset({"Bedroom"}), frozenset()))
    errs = [r["result"].get("error") for r in results(ev)]
    assert errs == [None, "out_of_scope", "out_of_scope", "out_of_scope"]                  # the Bedroom sensor ok; the rest refused
    assert [c["tool"] for c in env.fake.calls if c["tool"] != "list_devices"] == ["read_sensor"]
    assert not [c for c in env.fake.calls if c["tool"] == "list_devices" and c["args"].get("room") == "Hall"]


async def test_list_devices_result_is_filtered_to_the_scope(env):
    env.agent.llm.constrained = False
    once(env, [{"tool": "list_devices", "args": {}}])
    ctx, ev = await env.chat("what devices", scope=BEDROOM)
    assert one(ev, "tool_result")["result"]["count"] == 3                       # light, fan, temp sensor (not 6)
    once(env, [{"tool": "list_devices", "args": {}}])
    ctx, ev = await env.chat("what devices", scope=UNRESTRICTED)
    assert one(ev, "tool_result")["result"]["count"] == 6


async def test_scene_scope_by_id_and_by_name(env):
    env.agent.llm.constrained = False
    script(env, [{"tool": "run_scene", "args": {"scene": "Good night"}}, {"tool": "run_scene", "args": {"scene": "sc_9a8b7c6d"}}])
    ctx, ev = await env.chat("run it", scope=Scope(None, frozenset()))                   # rooms free, no scenes assigned
    assert [r["result"].get("error") for r in results(ev)] == ["out_of_scope"] * 2 and not hub_calls(env, "run_scene")
    # a scene id we have never listed, nor a name that is not ours, cannot be smuggled in either
    env.llm.script.clear()
    script(env, [{"tool": "run_scene", "args": {"scene": "sc_deadbeef"}}, {"tool": "run_scene", "args": {"scene": "Secret party"}}])
    ctx, ev = await env.chat("run it", scope=Scope(None, frozenset({"sc_9a8b7c6d"})))
    assert [r["result"].get("error") for r in results(ev)] == ["out_of_scope"] * 2 and not hub_calls(env, "run_scene")


async def test_in_scope_scene_runs_by_name_and_by_id(env):
    env.agent.llm.constrained = False
    script(env, [{"tool": "run_scene", "args": {"scene": "sc_9a8b7c6d"}}])
    sc = Scope(frozenset({"Bedroom", "Hall"}), frozenset({"sc_9a8b7c6d"}))
    ctx, ev = await env.chat("run it", scope=sc)
    assert "confirmation_request" in names(ev)                                         # risk:confirm lock inside: normal policy
    assert hub_calls(env, "run_scene")[0]["args"] == {"scene": "sc_9a8b7c6d"}
    env.agent.confirmations.resolve(env.agent.confirmations.pending_ids()[0], "alice", False) if env.agent.confirmations.pending_ids() else None


async def test_fast_path_cannot_name_out_of_scope_things(tmp_path):
    e = await make_env(tmp_path, sched={})
    try:
        ctx, ev = await e.chat("unlock the front door", scope=BEDROOM)
        assert not hub_calls(e, "set_state") and "confirmation_request" not in names(ev)
        assert e.llm.requests                                                              # unmatched in the scoped view: LLM turn
        ctx, ev = await e.chat("good night", scope=BEDROOM)                                 # scene not assigned: not even a candidate
        assert not hub_calls(e, "run_scene") and one(ev, "final")["route"] == "llm"
        ctx, ev = await e.chat("turn on the bedroom fan", scope=BEDROOM)                    # in scope: fast
        assert one(ev, "final")["route"] == "fast" and hub_calls(e, "set_state")[0]["args"]["endpoint_id"] == FAN
        ctx, ev = await e.chat("run good night scene", scope=BEDROOM_SCENE)
        assert one(ev, "final")["route"] == "fast" and hub_calls(e, "run_scene")
    finally:
        await e.close()


async def test_fast_path_plans_are_enforced_by_the_executor_too(tmp_path):
    """Defence in depth: even if a plan reaches run_calls for another room, the executor refuses it."""
    e = await make_env(tmp_path)
    try:
        ctx = e.agent.new_request("alice", "s", "x", "member", scope=BEDROOM)
        outs = await e.agent.executor.run_calls(ctx, [{"tool": "set_state", "args": {"endpoint_id": BLINDS, "capability": "position", "value": 0}}])
        assert outs[0].result["error"] == "out_of_scope" and not hub_calls(e, "set_state")
    finally:
        await e.close()


async def test_missing_scope_for_a_member_means_every_tool_is_out_of_scope(env):
    env.agent.llm.constrained = False
    script(env, [setstate(FAN, "power", True), {"tool": "read_sensor", "args": {"endpoint_id": SENSOR, "capability": "temperature"}}])
    ctx, ev = await env.chat("go", scope=make_scope("member"))
    assert [r["result"].get("error") for r in results(ev)] == ["out_of_scope"] * 2
    assert not hub_calls(env, "set_state", "read_sensor")


# ------------------------------------------------------------------------------------------------ approved retry path
async def test_approved_retry_is_scope_checked_too(env):
    ctx = env.agent.new_request("alice", "s", "x", "member", scope=BEDROOM)
    tok = env.fake.mint("alice", LOCK_ACTION)
    out = await env.agent.executor.run_approved(ctx, LOCK_ACTION, tok)
    assert out.result["error"] == "out_of_scope" and not hub_calls(env, "set_state")
    ctx = env.agent.new_request("alice", "s", "x", "member", scope=Scope(frozenset({"Hall"}), frozenset()))
    out = await env.agent.executor.run_approved(ctx, LOCK_ACTION, tok)
    assert out.ok and hub_calls(env, "set_state")
    scene = {"type": "run_scene", "scene_id": "sc_9a8b7c6d", "actions": []}
    ctx = env.agent.new_request("alice", "s", "x", "member", scope=Scope(None, frozenset()))
    assert (await env.agent.executor.run_approved(ctx, scene, "t")).result["error"] == "out_of_scope"


async def test_retry_handoff_uses_the_users_last_scope_and_fails_closed_without_one(env):
    """The decision body carries no scope: an unknown request id runs with the same user's latest chat scope, else nothing."""
    tok = env.fake.mint("alice", LOCK_ACTION)
    body = {"decision": "approved", "user_id": "alice", "role": "member", "action": LOCK_ACTION,
            "action_hash": action_hash(LOCK_ACTION), "confirmation_token": tok}
    assert (await env.agent.handle_decision("cr_x1", body))[0] == 202
    await asyncio.sleep(0.3)
    assert not hub_calls(env, "set_state")                                        # alice never chatted: nothing allowed
    await env.chat("hello", scope=Scope(frozenset({"Bedroom"}), frozenset()))      # chatted, but not allowed in Hall
    body["confirmation_token"] = tok = env.fake.mint("alice", LOCK_ACTION)
    assert (await env.agent.handle_decision("cr_x2", body))[0] == 202
    await asyncio.sleep(0.3)
    assert not hub_calls(env, "set_state")
    await env.chat("hello", scope=Scope(frozenset({"Hall"}), frozenset()))
    body["confirmation_token"] = env.fake.mint("alice", LOCK_ACTION)
    assert (await env.agent.handle_decision("cr_x3", body))[0] == 202
    for _ in range(40):
        if hub_calls(env, "set_state"):
            break
        await asyncio.sleep(0.05)
    assert len(hub_calls(env, "set_state")) == 1


# ------------------------------------------------------------------------------------------------ guests (S8b kept)
async def test_guest_still_never_sets_state_or_runs_scenes_even_in_scope(env):
    env.agent.llm.constrained = False
    script(env, [setstate(FAN, "power", True), {"tool": "run_scene", "args": {"scene": "sc_9a8b7c6d"}}])
    ctx, ev = await env.chat("go", user="visitor", role="guest", scope=GUEST_SCOPE)
    assert [r["result"].get("error") for r in results(ev)] == ["not_allowed"] * 2
    assert not hub_calls(env, "set_state", "run_scene")
    assert sorted(r["tool"] for r in env.agent.store.action_log(50) if r["status"] == "denied_guest") == ["run_scene", "set_state"]


async def test_guest_reads_only_inside_assigned_rooms(env):
    env.agent.llm.constrained = False
    s = {"tool": "read_sensor", "args": {"endpoint_id": SENSOR, "capability": "temperature"}}
    once(env, [s])
    ctx, ev = await env.chat("temp", user="visitor", role="guest", scope=Scope(frozenset({"Bedroom"}), frozenset()))
    assert results(ev)[0]["ok"] is True and hub_calls(env, "read_sensor")
    env.llm.script.clear()
    env.fake.calls.clear()
    once(env, [s])
    ctx, ev = await env.chat("temp", user="visitor", role="guest", scope=Scope(frozenset({"Hall"}), frozenset()))
    assert results(ev)[0]["result"]["error"] == "out_of_scope" and not hub_calls(env, "read_sensor")


async def test_a_guest_scope_is_never_unrestricted_even_if_a_caller_passes_one(env):
    ctx = env.agent.new_request("visitor", "s", "x", "guest", scope=UNRESTRICTED)
    assert ctx.scope.rooms == frozenset() and ctx.scope.scenes == frozenset()
    ctx = env.agent.new_request("visitor", "s", "x", "guest")
    assert ctx.scope.restricted
    out = await env.agent.executor.run_call(ctx, "read_sensor", {"endpoint_id": SENSOR, "capability": "temperature"})
    assert out.result["error"] == "out_of_scope"


# ------------------------------------------------------------------------------------------------ nothing leaks to the model
async def test_out_of_scope_names_never_reach_the_model(env):
    ctx, ev = await env.chat("hello there", scope=BEDROOM)
    sent = json.dumps(env.llm.requests[-1])
    assert "Fan" in sent and "Front door" not in sent and "Blinds" not in sent and "Hall" not in sent
    assert "Good night" not in sent                                              # scene not assigned
    ctx, ev = await env.chat("hello there", scope=BEDROOM_SCENE)
    assert "Good night" in json.dumps(env.llm.requests[-1])
    ctx, ev = await env.chat("hello there")                                      # unrestricted: the shared prefix, everything
    assert "Front door" in json.dumps(env.llm.requests[-1])


async def test_decoding_schema_is_scoped_and_the_unrestricted_prefix_stays_stable(env):
    full = env.agent.plan_schema(UNRESTRICTED)
    scoped = env.agent.plan_schema(BEDROOM)
    assert LOCK in schema_endpoint_ids(full) and LOCK not in schema_endpoint_ids(scoped) and FAN in schema_endpoint_ids(scoped)
    assert "Good night" not in json.dumps(scoped) and "Good night" in json.dumps(full)
    assert env.agent.plan_schema(BEDROOM) is scoped and env.agent.plan_schema(UNRESTRICTED) is full      # cached per scope
    before = env.agent.builder.prefix
    await env.chat("hello", scope=BEDROOM)
    await env.chat("hello", scope=GUEST_SCOPE)
    assert env.agent.builder.prefix == before                                    # restricted callers never touch the shared prefix


# ------------------------------------------------------------------------------------------------ registry_version + scenes
async def test_cache_is_keyed_on_registry_version_and_falls_back_to_the_content_hash(tmp_path):
    e = await make_env(tmp_path)
    try:
        assert e.agent.registry.list_version == e.fake.version
        first = e.agent.plan_schema()
        assert first.key.startswith("v") and e.agent.plan_schema() is first
        builds = e.agent.sched.constraints.builds
        e.fake.rename(FAN, "Ventilator")                                          # version bump (a label-only change)
        for _ in range(100):
            if e.agent.registry.list_version == e.fake.version:
                break
            await asyncio.sleep(0.02)
        second = e.agent.plan_schema()
        assert second is not first and second.key != first.key and e.agent.sched.constraints.builds == builds + 1
        await e.agent.loader.reload()                                             # node_status-style reload: same version, no rebuild
        assert e.agent.plan_schema() is second
    finally:
        await e.close()
    e = await make_env(tmp_path)
    try:
        e.fake.send_registry_version = False                                      # a hub without registry_version
        await e.agent.loader.reload()
        assert e.agent.registry.list_version is None and not e.agent.registry.scenes_known
        sch = e.agent.plan_schema()
        assert sch.key.startswith("c")                                            # content hash
        e.fake.rename(FAN, "Ventilator")
        await asyncio.sleep(0.3)
        assert e.agent.plan_schema() is sch                                       # label-only: compiled artefacts kept
    finally:
        await e.close()


async def test_scenes_come_from_list_devices_not_from_config(tmp_path):
    e = await make_env(tmp_path)
    try:
        assert [s.name for s in e.agent.registry.scenes.values()] == ["Good night"]
        assert e.agent.registry.scenes["sc_9a8b7c6d"].broken is False
        assert "Good night" in e.agent.builder.prefix
        enum = json.dumps(e.agent.plan_schema())
        assert '"Good night"' in enum
        with pytest.raises(TypeError):
            from agent.config import AgentConfig
            AgentConfig(scene_names=["x"])                                         # the workaround is gone
    finally:
        await e.close()


def test_malformed_scene_entries_and_registry_versions_are_dropped():
    reg = DeviceRegistry()
    reg.replace([], None, [{"scene_id": "sc_1", "name": "A"}, {"scene_id": "sc_2"}, {"name": "B"}, "x",
                           {"scene_id": "bad id!", "name": "C"}, {"scene_id": "sc_3", "name": "Broke", "broken": True}], 7)
    assert sorted(reg.scenes) == ["sc_1", "sc_3"] and reg.scene_names() == ("A",) and reg.list_version == 7
    reg.replace([], 3, None, None)
    assert reg.scenes == {} and not reg.scenes_known and reg.list_version is None and reg.version == 3


# ------------------------------------------------------------------------------------------------ route enum
async def test_route_values_follow_the_extended_enum(tmp_path):
    e = await make_env(tmp_path, sched={})
    try:
        seen = {}
        for label, text in (("fast", "turn on the bedroom fan"), ("sensor", "what is the temperature in the bedroom?"),
                            ("llm", "hello there")):
            ctx, ev = await e.chat(text)
            seen[label] = one(ev, "final")["route"]
        assert seen == {"fast": "fast", "sensor": "sensor", "llm": "llm"}
        from agent.sched.router import BackendAnswer

        class Backend:
            def __init__(self, src):
                self.src = src

            def supports(self, s):
                return s == self.src

            async def answer(self, s, q, images):
                return BackendAnswer(True, "Yes.", 50.0)

        e.agent.sched.router.avail.register(Backend("yolo_pi"))
        ctx = e.agent.new_request("alice", "s1", "is there a person at the door?", images=1)
        assert one(await collect(ctx), "final")["route"] == "yolo"
        assert e.agent.store.traces(1)[0]["detail"]["sse_route"] == "yolo"
        e.agent.sched.router.avail.backends.clear()
        e.agent.sched.router.avail.register(Backend("vlm_yesno"))
        ctx = e.agent.new_request("alice", "s1", "is the porch light on in the picture?", images=1)
        ev = await collect(ctx)
        assert one(ev, "final")["route"] == "vlm"
        routes = {t["detail"]["sse_route"] for t in e.agent.store.traces(20)}
        assert routes <= {"fast", "llm", "sensor", "yolo", "vlm"} and "vision" not in routes
    finally:
        await e.close()
