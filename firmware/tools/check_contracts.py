"""Validate what the example firmware would publish against the contract schemas.

Input: a directory of <example>.<kind>.json files written by `pio test -e native -f test_examples`
(kinds: announce, state, cmd, ack-ok, ack-err, ack-identify).
Checks: JSON Schema (announce, state, cmd, ack) plus the hub-side rules the schema cannot express
(contracts/capabilities.md, "Hub-enforced rules").
"""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

out_dir = Path(sys.argv[1])
contracts = Path(sys.argv[2])

SCHEMAS = {
    "announce": "announce.schema.json",
    "state": "state.schema.json",
    "cmd": "cmd.schema.json",
    "ack-ok": "ack.schema.json",
    "ack-err": "ack.schema.json",
    "ack-identify": "ack.schema.json",
}
validators = {}
for kind, fname in SCHEMAS.items():
    schema = json.loads((contracts / fname).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validators[kind] = Draft202012Validator(schema)

failures = 0
checked = 0


def fail(msg):
    global failures
    failures += 1
    print("FAIL", msg)


def hub_rules(name, ann):
    """Cross-field rules the hub enforces on an announce (capabilities.md)."""
    if len(json.dumps(ann).encode()) > 16 * 1024:
        fail(f"{name}: announce > 16 KiB")
    local_ids = set()
    for ep in ann["endpoints"]:
        if ep["local_id"] in local_ids:
            fail(f"{name}: duplicate local_id {ep['local_id']}")
        local_ids.add(ep["local_id"])
        cap_ids = set()
        for c in ep["capabilities"]:
            if c["id"] in cap_ids:
                fail(f"{name}: duplicate capability {c['id']} on {ep['local_id']}")
            cap_ids.add(c["id"])
            if "min" in c and "max" in c and c["min"] > c["max"]:
                fail(f"{name}: min > max on {c['id']}")
            for key in ("boot_value", "failsafe_value"):
                if key not in c:
                    continue
                v = c[key]
                if c["type"] in ("int", "float") and "min" in c and not (c["min"] <= v <= c["max"]):
                    fail(f"{name}: {key} {v} outside [{c['min']},{c['max']}] on {c['id']}")
                if c["type"] == "enum" and v not in c["values"]:
                    fail(f"{name}: {key} {v!r} not in enum values on {c['id']}")


def state_matches_announce(name, ann, state):
    declared = {ep["local_id"]: {c["id"]: c for c in ep["capabilities"]} for ep in ann["endpoints"]}
    for ep, caps in state["endpoints"].items():
        if ep not in declared:
            fail(f"{name}: state has unknown endpoint {ep}")
            continue
        for cid, val in caps.items():
            c = declared[ep].get(cid)
            if not c:
                fail(f"{name}: state has undeclared capability {ep}/{cid}")
                continue
            t = c["type"]
            ok = (
                (t == "bool" and isinstance(val, bool))
                or (t == "int" and isinstance(val, int) and not isinstance(val, bool))
                or (t == "float" and isinstance(val, (int, float)) and not isinstance(val, bool))
                or (t == "enum" and val in c.get("values", []))
            )
            if not ok:
                fail(f"{name}: state value {val!r} has the wrong type for {ep}/{cid} ({t})")


files = sorted(out_dir.glob("*.json"))
if not files:
    print("FAIL: no files in", out_dir)
    sys.exit(1)

by_example = {}
for f in files:
    example, kind, _ = f.name.rsplit(".", 2)
    data = json.loads(f.read_text(encoding="utf-8"))
    by_example.setdefault(example, {})[kind] = data
    errors = list(validators[kind].iter_errors(data))
    checked += 1
    if errors:
        fail(f"{f.name}: {errors[0].message[:140]}")
    else:
        print(f"ok   {f.name}")

for name, kinds in by_example.items():
    if "announce" not in kinds:
        fail(f"{name}: no announce")
        continue
    hub_rules(name, kinds["announce"])
    if "state" in kinds:
        state_matches_announce(name, kinds["announce"], kinds["state"])

print(f"{checked} files checked, {failures} failure(s)")
sys.exit(1 if failures else 0)
