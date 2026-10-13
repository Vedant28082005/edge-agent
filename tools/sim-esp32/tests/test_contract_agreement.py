"""The sim's vocabulary and every message it can build agree with contracts/ (no drift)."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from simesp32.config import ConfigError, load_config
from simesp32.schemas import SchemaViolation, default_contracts_dir
from simesp32.vocab import ENDPOINT_TYPES, VOCAB

CONTRACTS = default_contracts_dir()
ROOT = Path(__file__).resolve().parents[1]


def test_vocab_matches_announce_schema():
    schema = json.loads((CONTRACTS / "announce.schema.json").read_text(encoding="utf-8"))
    cap = schema["$defs"]["capability"]
    assert sorted(cap["properties"]["id"]["enum"]) == sorted(VOCAB)
    assert schema["$defs"]["endpoint"]["properties"]["type"]["enum"] == ENDPOINT_TYPES
    rules = {}
    for r in cap["allOf"]:
        cid = r["if"]["properties"].get("id", {}).get("const")
        if cid:
            rules[cid] = r["then"]["properties"]
    assert set(rules) == set(VOCAB)
    for cid, (kind, typ, *_rest) in VOCAB.items():
        assert rules[cid]["kind"]["const"] == kind, cid
        assert rules[cid]["type"]["const"] == typ, cid


@pytest.mark.parametrize("name", ["home3.yaml", "faults.yaml"])
def test_examples_load_and_announce_validates(validator, name):
    for n in load_config(ROOT / "examples" / name):
        if n.faults.malformed_announce is None:
            validator.validate("announce", n.announce())


def test_every_vocab_capability_can_be_announced(validator):
    for cid, (kind, typ, lo, hi, fixed) in VOCAB.items():
        cap = {"id": cid}
        if kind == "actuator":
            cap["boot_value"] = {"bool": False, "int": 1 if lo is None else lo, "float": 20.0,
                                 "enum": (fixed or ["a", "b"])[0]}[typ]
            if cid == "speed":
                cap.update(min=1, max=5)
            if cid == "mode":
                cap["values"] = ["a", "b"]
        else:
            cap["interval_s"] = 10
        from simesp32.config import _node
        n = _node({"node_id": "esp32-000000000001", "endpoints": [
            {"local_id": "e1", "type": "generic", "capabilities": [cap]}]}, 0)
        validator.validate("announce", n.announce())


def test_validator_rejects_bad_messages(validator):
    with pytest.raises(SchemaViolation):
        validator.validate("state", {"ts": 5, "endpoints": {"a": {"power": True}}})
    with pytest.raises(SchemaViolation):
        validator.validate("ack", {"cmd_id": "x", "ok": True, "endpoint": None, "values": {}, "error": None})
    with pytest.raises(SchemaViolation):
        validator.validate("announce", {"schema": 1})


@pytest.mark.parametrize("bad", [
    {"node_id": "esp32-a1b2c3d4e501", "endpoints": [{"local_id": "x", "type": "light", "capabilities": [
        {"id": "teleport", "boot_value": False}]}]},
    {"node_id": "esp32-a1b2c3d4e501", "endpoints": [{"local_id": "x", "type": "light", "name": "Kitchen",
        "capabilities": [{"id": "power", "boot_value": False}]}]},
    {"node_id": "esp32-a1b2c3d4e501", "endpoints": [{"local_id": "x", "type": "light", "capabilities": [
        {"id": "brightness", "boot_value": 500}]}]},
    {"node_id": "esp32-a1b2c3d4e501", "endpoints": [{"local_id": "x", "type": "sensor", "capabilities": [
        {"id": "temperature", "boot_value": 1, "interval_s": 5}]}]},
    {"node_id": "bad", "endpoints": []},
])
def test_config_rejects_what_the_hub_would(bad):
    from simesp32.config import _node
    with pytest.raises(ConfigError):
        _node(bad, 0)
