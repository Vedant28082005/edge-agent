"""Registry-derived constrained decoding: golden schema/grammar, grammar behaviour (via a GBNF->regex translation, the real
server is unverified), agreement with the executor's validator, cache by registry content, backend request shapes."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import jsonschema
import pytest
from gbnf_regex import to_regex
from sched_fixtures import DEVICES, SCENES, registry

from agent.llm import LlmClient
from agent.schema import build_plan_schema, validate_call
from agent.sched.grammar import (ConstraintCache, GbnfConstraint, GbnfError, JsonSchemaConstraint, make_backend,
                                 registry_decoding_key, schema_to_gbnf)

GOLDEN = Path(__file__).parent / "golden"


def dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def golden(name: str, text: str) -> None:
    p = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN") == "1" or not p.exists():
        p.write_text(text, encoding="utf-8", newline="\n")
    assert p.read_text(encoding="utf-8") == text, f"{name} changed; review it and re-run with UPDATE_GOLDEN=1"


def plan(*calls, say="ok") -> str:
    return json.dumps({"calls": list(calls), "say": say}, separators=(",", ":"))


def S(ep, cap, v):
    return {"tool": "set_state", "args": {"endpoint_id": ep, "capability": cap, "value": v}}


@pytest.fixture(scope="module")
def reg():
    return registry()


@pytest.fixture(scope="module")
def schema(reg):
    return build_plan_schema(reg, SCENES)


@pytest.fixture(scope="module")
def rx(schema):
    return to_regex(schema_to_gbnf(schema))


# ---------------------------------------------------------------------------------------------------- golden
def test_golden_schema_and_grammar(schema):
    golden("plan_schema.json", dump(schema))
    golden("plan_schema.gbnf", schema_to_gbnf(schema))


def test_grammar_is_deterministic(schema):
    assert schema_to_gbnf(schema) == schema_to_gbnf(copy.deepcopy(schema))


# ---------------------------------------------------------------------------------------------------- behaviour
VALID = [
    plan(),
    plan(S("ep_b0000002", "power", True)),
    plan(S("ep_b0000001", "brightness", 0), S("ep_b0000001", "brightness", 100), say="Dimming."),
    plan(S("ep_b0000002", "speed", 5)),
    plan(S("ep_b0000007", "speed", 3)),
    plan(S("ep_b0000006", "lock", "unlocked")),
    plan(S("ep_b0000005", "position", 50)),
    plan({"tool": "read_sensor", "args": {"endpoint_id": "ep_b0000003", "capability": "temperature"}}),
    plan({"tool": "read_sensor", "args": {"endpoint_id": "ep_b000000a", "capability": "motion"}}),
    plan({"tool": "get_state", "args": {"endpoint_id": "ep_b0000004"}}),
    plan({"tool": "get_state", "args": {"room": "Kitchen"}}),
    plan({"tool": "list_devices", "args": {}}),
    plan({"tool": "list_devices", "args": {"room": "Bedroom"}}),
    plan({"tool": "list_devices", "args": {"type": "fan"}}),
    plan({"tool": "list_devices", "args": {"room": "Hall", "type": "lock"}}),
    plan({"tool": "run_scene", "args": {"scene": "Good night"}}),
    plan(*[S("ep_b0000002", "power", False)] * 4),
]
INVALID = [
    ("unknown endpoint", plan(S("ep_dead0000", "power", True))),
    ("capability the device lacks", plan(S("ep_b0000008", "brightness", 10))),
    ("sensor as actuator", plan(S("ep_b0000003", "temperature", 20))),
    ("brightness above range", plan(S("ep_b0000001", "brightness", 101))),
    ("negative brightness", plan(S("ep_b0000001", "brightness", -1))),
    ("fan speed above its own max", plan(S("ep_b0000007", "speed", 4))),
    ("fan speed zero", plan(S("ep_b0000002", "speed", 0))),
    ("lock value outside the enum", plan(S("ep_b0000006", "lock", "open"))),
    ("string for a bool", plan(S("ep_b0000002", "power", "on"))),
    ("float for an int", plan(S("ep_b0000001", "brightness", 50.5))),
    ("five calls", plan(*[S("ep_b0000002", "power", True)] * 5)),
    ("unknown tool", plan({"tool": "delete_device", "args": {"endpoint_id": "ep_b0000002"}})),
    ("extra argument", plan({"tool": "get_state", "args": {"endpoint_id": "ep_b0000004", "token": "x"}})),
    ("unknown room", plan({"tool": "get_state", "args": {"room": "Garage"}})),
    ("unknown scene", plan({"tool": "run_scene", "args": {"scene": "Party"}})),
    ("read_sensor on an actuator capability", plan({"tool": "read_sensor", "args": {"endpoint_id": "ep_b0000002", "capability": "power"}})),
    ("prose instead of a plan", "Sure, turning it on!"),
    ("missing say", json.dumps({"calls": []})),
]


@pytest.mark.parametrize("text", VALID, ids=[f"valid{i}" for i in range(len(VALID))])
def test_valid_plans_match_the_schema_and_the_grammar(schema, rx, text):
    jsonschema.validate(json.loads(text), schema)
    assert rx.fullmatch(text), text


@pytest.mark.parametrize("why,text", INVALID, ids=[w for w, _ in INVALID])
def test_invalid_plans_are_impossible_in_the_grammar(schema, rx, why, text):
    assert not rx.fullmatch(text), why
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, schema)


def test_grammar_agrees_with_the_executors_validator_on_a_grid(reg, schema, rx):
    checked = 0
    for ep in sorted(reg.devices):
        d = reg.devices[ep]
        for c in d.caps:
            if c.kind != "actuator":
                continue
            values = [True, False, "locked", "unlocked", "open", "x"] if c.type in ("bool", "enum") else list(range(-2, 105))
            for v in values:
                text = plan(S(ep, c.id, v))
                err, _ = validate_call(reg, "set_state", {"endpoint_id": ep, "capability": c.id, "value": v})
                assert bool(rx.fullmatch(text)) == (err is None), (ep, c.id, v, err)
                checked += 1
    assert checked > 400


def test_every_endpoint_the_grammar_can_name_exists(reg, schema):
    from agent.schema import schema_endpoint_ids
    assert schema_endpoint_ids(schema) <= set(reg.devices)


def test_unsupported_schema_is_refused_not_guessed():
    with pytest.raises(GbnfError):
        schema_to_gbnf({"type": "null"})
    with pytest.raises(GbnfError):
        schema_to_gbnf({"enum": []})


# ---------------------------------------------------------------------------------------------------- cache
def test_cache_rebuilds_only_on_decoding_relevant_changes():
    cache, reg = ConstraintCache(), registry()
    a = cache.get(reg, SCENES)
    assert cache.get(reg, SCENES) is a and (cache.builds, cache.hits) == (1, 1)

    renamed = copy.deepcopy(DEVICES)
    renamed[1]["name"] = "Ventilator"
    renamed[1]["tags"] = ["cool"]
    reg.replace(renamed, 2)
    assert cache.get(reg, SCENES) is a and cache.builds == 1             # a label edit does not touch the schema
    assert a.hub_version == 2

    ranged = copy.deepcopy(DEVICES)
    ranged[1]["capabilities"][1]["max"] = 4                               # fan speed range changed
    reg.replace(ranged, 3)
    b = cache.get(reg, SCENES)
    assert b is not a and cache.builds == 2 and b.key != a.key
    assert "ep_b0000002" in json.dumps(b) and '"maximum": 4' in json.dumps(b, indent=1).replace('"maximum":4', '"maximum": 4')

    reg.replace(DEVICES[:-1], 4)                                          # a device forgotten
    c = cache.get(reg, SCENES)
    assert c.key not in (a.key, b.key) and cache.builds == 3
    assert "ep_b000000a" not in json.dumps(c)

    d = cache.get(reg, SCENES + ("Party",))                               # scene list is part of the key
    assert d.key != c.key and cache.builds == 4


def test_key_ignores_online_flag_and_ordering():
    shuffled = copy.deepcopy(DEVICES)[::-1]
    for dev in shuffled:
        dev["online"] = False
    assert registry_decoding_key(registry(shuffled), SCENES) == registry_decoding_key(registry(), SCENES)


def test_cached_schema_carries_its_gbnf(reg):
    s = ConstraintCache().get(reg, SCENES)
    assert s.gbnf == schema_to_gbnf(s) and s.key


# ---------------------------------------------------------------------------------------------------- backends / request shape
def test_backend_request_shapes(schema):
    sch = ConstraintCache().get(registry(), SCENES)
    body: dict = {}
    JsonSchemaConstraint().apply(body, sch)
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == dict(sch)
    assert "grammar" not in body
    body = {}
    GbnfConstraint().apply(body, sch)
    assert body["grammar"] == sch.gbnf and "response_format" not in body
    assert make_backend("gbnf").name == "gbnf" and make_backend("json_schema").name == "json_schema"


def test_llm_payload_follows_the_flags():
    sch = ConstraintCache().get(registry(), SCENES)
    llm = LlmClient("http://127.0.0.1:1")
    msgs = [{"role": "system", "content": "x"}]
    p = llm.payload(msgs, sch)
    assert p["cache_prompt"] is True and p["id_slot"] == 0 and "response_format" in p
    llm.id_slot = 2
    assert llm.payload(msgs, sch)["id_slot"] == 2
    llm.constraint = GbnfConstraint()
    p = llm.payload(msgs, sch)
    assert "grammar" in p and "response_format" not in p
    llm.constrained = False                                               # ablation: no constraint at all
    p = llm.payload(msgs, sch)
    assert "grammar" not in p and "response_format" not in p
    llm.prefix_cache = False                                              # ablation: no slot pinning, no cache_prompt
    p = llm.payload(msgs, sch)
    assert p["cache_prompt"] is False and "id_slot" not in p
