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

from agent.aliases import build_aliases
from agent.llm import LlmClient
from agent.schema import build_plan_schema, validate_call
from agent.sched.grammar import (ConstraintCache, GbnfConstraint, GbnfError, JsonSchemaConstraint, make_backend,
                                 registry_decoding_key, schema_to_gbnf)

GOLDEN = Path(__file__).parent / "golden"
# sched_fixtures.DEVICES ids sort to e1..e10 in numeric order (b0000001..b000000a): Ceiling light(Bedroom)=e1,
# Fan(Bedroom)=e2, Temp sensor=e3, Lamp=e4, Blinds=e5, Front door=e6, Fan(Living room)=e7, Ceiling light(Kitchen)=e8,
# Humidity sensor=e9, Motion sensor=e10.
E1, E2, E3, E4, E5, E6, E7, E8 = "e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8"
E10 = "e10"


def dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def golden(name: str, text: str) -> None:
    p = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN") == "1" or not p.exists():
        p.write_text(text, encoding="utf-8", newline="\n")
    assert p.read_text(encoding="utf-8") == text, f"{name} changed; review it and re-run with UPDATE_GOLDEN=1"


def plan(*calls) -> str:
    return json.dumps(list(calls), separators=(",", ":"))


def S(alias, cap, v):
    return ["s", alias, cap, v]


@pytest.fixture(scope="module")
def reg():
    return registry()


@pytest.fixture(scope="module")
def aliases(reg):
    return build_aliases(reg)


@pytest.fixture(scope="module")
def schema(reg, aliases):
    return build_plan_schema(reg, aliases, SCENES)


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
    plan(S(E2, "power", True)),
    plan(S(E1, "brightness", 0), S(E1, "brightness", 100)),
    plan(S(E2, "speed", 5)),
    plan(S(E7, "speed", 3)),
    plan(S(E6, "lock", "unlocked")),
    plan(S(E5, "position", 50)),
    plan(["r", E3, "temperature"]),
    plan(["r", E10, "motion"]),
    plan(["g", E4]),
    plan(["gr", "Kitchen"]),
    plan(["l", None, None]),
    plan(["l", "Bedroom", None]),
    plan(["l", None, "fan"]),
    plan(["l", "Hall", "lock"]),
    plan(["n", "Good night"]),
    plan(*[S(E2, "power", False)] * 4),
]
INVALID = [
    ("unknown alias", plan(S("e99", "power", True))),
    ("capability the device lacks", plan(S(E8, "brightness", 10))),
    ("sensor as actuator", plan(S(E3, "temperature", 20))),
    ("brightness above range", plan(S(E1, "brightness", 101))),
    ("negative brightness", plan(S(E1, "brightness", -1))),
    ("fan speed above its own max", plan(S(E7, "speed", 4))),
    ("fan speed zero", plan(S(E2, "speed", 0))),
    ("lock value outside the enum", plan(S(E6, "lock", "open"))),
    ("string for a bool", plan(S(E2, "power", "on"))),
    ("float for an int", plan(S(E1, "brightness", 50.5))),
    ("five calls", plan(*[S(E2, "power", True)] * 5)),
    ("unknown tool code", plan(["z", E2])),
    ("extra tuple item", plan(["g", E4, "x"])),
    ("unknown room", plan(["gr", "Garage"])),
    ("unknown scene", plan(["n", "Party"])),
    ("read_sensor on an actuator capability", plan(["r", E2, "power"])),
    ("prose instead of a plan", "Sure, turning it on!"),
    ("an object, not a bare array", json.dumps({"calls": []})),
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


def test_grammar_agrees_with_the_executors_validator_on_a_grid(reg, aliases, schema, rx):
    checked = 0
    for ep in sorted(reg.devices):
        d = reg.devices[ep]
        for c in d.caps:
            if c.kind != "actuator":
                continue
            values = [True, False, "locked", "unlocked", "open", "x"] if c.type in ("bool", "enum") else list(range(-2, 105))
            for v in values:
                text = plan(S(aliases.alias(ep), c.id, v))
                err, _ = validate_call(reg, "set_state", {"endpoint_id": ep, "capability": c.id, "value": v})
                assert bool(rx.fullmatch(text)) == (err is None), (ep, c.id, v, err)
                checked += 1
    assert checked > 400


def test_every_endpoint_the_grammar_can_name_exists(reg, aliases, schema):
    from agent.schema import schema_endpoint_ids
    assert schema_endpoint_ids(schema, aliases) <= set(reg.devices)


def test_unsupported_schema_is_refused_not_guessed():
    with pytest.raises(GbnfError):
        schema_to_gbnf({"not": "a schema node"})
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
    assert b.aliases.alias("ep_b0000002") in json.dumps(b) and '"maximum": 4' in json.dumps(b, indent=1).replace('"maximum":4', '"maximum": 4')

    reg.replace(DEVICES[:-1], 4)                                          # a device forgotten
    c = cache.get(reg, SCENES)
    assert c.key not in (a.key, b.key) and cache.builds == 3
    assert "ep_b000000a" not in c.aliases.to_alias

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
