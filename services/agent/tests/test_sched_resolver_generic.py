"""(S8g) Fast-path generality: driven by the endpoint's announced capabilities (type, kind, min/max, enum values),
never by a hard-coded device name or type, so a brand new device works with no agent change.

- open/close resolves to any endpoint's `position` capability, not only `type == "cover"`.
- a `mode` (or other enum) capability is matched against the registry's own announced values.
- "raise/lower ... by N" (relative) is identified by the resolver (no I/O there); the live-state fetch and the
  actual computation happen in the scheduler, tested end to end through the real `Agent` + fake hub."""
from __future__ import annotations

from conftest import SECRET, Env, one

from agent.config import AgentConfig
from agent.devices import DeviceRegistry
from agent.mock import FakeHub, LocalServer, ScriptedLLM
from agent.sched.resolver import FALLTHROUGH, PLAN, RELATIVE_PENDING, FastPathResolver
from agent.service import Agent


def _cap(cid, kind, typ, **kw):
    return {"id": cid, "kind": kind, "type": typ, **kw}


THERMOSTAT_DEVICES = [
    {"endpoint_id": "ep_c0000001", "name": "Thermostat", "room": "Hall", "type": "thermostat", "tags": [],
     "online": True, "risk": "normal",
     "capabilities": [_cap("power", "actuator", "bool"),
                      _cap("mode", "actuator", "enum", values=["cool", "heat", "off"]),
                      _cap("target_temp", "actuator", "float", min=10, max=30)]},
    # a non-"cover" endpoint type that still announces `position` -- open/close must still resolve to it
    {"endpoint_id": "ep_c0000002", "name": "Awning", "room": "Patio", "type": "generic", "tags": [],
     "online": True, "risk": "normal", "capabilities": [_cap("position", "actuator", "int", min=0, max=100)]},
]


def reg():
    r = DeviceRegistry()
    r.replace(THERMOSTAT_DEVICES, 1)
    return r


async def custom_env(tmp_path, devices):
    """Like conftest's `make_env`, but with the resolver's generic fast path enabled and a custom device set
    (`make_env` only knows the demo registry)."""
    secret_file = tmp_path / "agent.secret"
    secret_file.write_text(SECRET)
    fake = FakeHub(SECRET, devices=devices, settle_delay=0.05)
    server = LocalServer(fake.app())
    url = await server.start()
    cfg = AgentConfig(hub_url=url, hub_secret_file=str(secret_file), db_path=":memory:", confirmation_ttl_s=2.0,
                      settle_wait_s=2.0, warm_prefix=False, sched={})
    llm = ScriptedLLM(cfg.constrained)
    agent = Agent.from_config(cfg, llm)
    await agent.start()
    return Env(agent, fake, llm, server, tmp_path)


def test_open_close_resolves_position_on_a_non_cover_type():
    res = FastPathResolver().resolve("open the awning", reg(), "member", ())
    assert res.status == PLAN
    assert res.calls == [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000002", "capability": "position", "value": 100}}]


def test_mode_enum_matched_from_the_live_registry_not_a_fixed_word_list():
    res = FastPathResolver().resolve("set the thermostat to cool", reg(), "member", ())
    assert res.status == PLAN
    calls = [c["args"]["capability"] for c in res.calls]
    assert calls == ["power", "mode"]
    assert res.calls[1]["args"] == {"endpoint_id": "ep_c0000001", "capability": "mode", "value": "cool"}


def test_mode_to_off_never_implies_power_on():
    res = FastPathResolver().resolve("set the thermostat to off", reg(), "member", ())
    assert res.status == PLAN
    assert res.calls == [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000001", "capability": "mode", "value": "off"}}]


def test_relative_command_identifies_intent_without_reading_live_state():
    """The resolver never does I/O: a relative command on a named device resolves to RELATIVE_PENDING, carrying the
    endpoint/capability/direction for the scheduler -- not a guessed value."""
    res = FastPathResolver().resolve("raise the thermostat temperature by 2", reg(), "member", ())
    assert res.status == RELATIVE_PENDING
    ep, cap, direction, amount, pct = res.relative
    assert (ep, cap, direction, amount) == ("ep_c0000001", "target_temp", 1, 2.0)


def test_relative_pronoun_only_still_falls_through_unchanged():
    for text in ("make it brighter", "turn it up"):
        res = FastPathResolver().resolve(text, reg(), "member", ())
        assert res.status == FALLTHROUGH and res.reason == "relative"


async def test_relative_command_runs_end_to_end_through_get_state_then_set_state(tmp_path):
    """Scheduler-level: fetches the current value with the ordinary `get_state` tool, computes and clamps the new
    target against the capability's own announced range, then runs `set_state` -- same executor, same validation."""
    e = await custom_env(tmp_path, THERMOSTAT_DEVICES)
    try:
        e.fake.state["ep_c0000001"]["target_temp"] = 20.0
        ctx, ev = await e.chat("raise the thermostat temperature by 2")
        final = one(ev, "final")
        assert final["route"] == "fast" and e.llm.requests == []
        sets = [c for c in e.fake.calls if c["tool"] == "set_state"]
        # the generic power-dependency rule applies here too: target_temp is not power/lock and 22.0 is not off-like
        assert [c["args"]["capability"] for c in sets] == ["power", "target_temp"]
        assert sets[1]["args"] == {"endpoint_id": "ep_c0000001", "capability": "target_temp", "value": 22.0}
    finally:
        await e.close()


async def test_relative_command_clamps_to_the_announced_range(tmp_path):
    e = await custom_env(tmp_path, THERMOSTAT_DEVICES)
    try:
        e.fake.state["ep_c0000001"]["target_temp"] = 29.0
        ctx, ev = await e.chat("raise the thermostat temperature by 5")
        one(ev, "final")
        c = next(c for c in e.fake.calls if c["tool"] == "set_state" and c["args"]["capability"] == "target_temp")
        assert c["args"]["value"] == 30.0                 # clamped to the announced max, never guessed past it
    finally:
        await e.close()


async def test_relative_command_falls_through_to_the_llm_when_state_is_unknown(tmp_path):
    """No seeded value for this endpoint (as if it were offline/never reported): never guess, let the LLM take it."""
    e = await custom_env(tmp_path, THERMOSTAT_DEVICES)
    try:
        ctx, ev = await e.chat("raise the thermostat temperature by 2")
        final = one(ev, "final")
        assert final["route"] == "llm"
        assert not [c for c in e.fake.calls if c["tool"] == "set_state"]
    finally:
        await e.close()
