"""Validate contracts/samples/{valid,invalid}/*.json against announce.schema.json."""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

contracts = Path(sys.argv[1])
schema = json.loads((contracts / "announce.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(schema)
validator = Draft202012Validator(schema)

failures = 0
counts = {"valid": 0, "invalid": 0}
for kind in ("valid", "invalid"):
    files = sorted((contracts / "samples" / kind).glob("*.json"))
    if not files:
        print(f"FAIL: no samples in {kind}/")
        failures += 1
    for f in files:
        counts[kind] += 1
        errors = list(validator.iter_errors(json.loads(f.read_text(encoding="utf-8"))))
        ok = (not errors) if kind == "valid" else bool(errors)
        if ok:
            note = "" if kind == "valid" else f"  (rejected: {errors[0].message[:70]})"
            print(f"ok   {kind}/{f.name}{note}")
        else:
            failures += 1
            why = "expected to pass but failed: " + errors[0].message[:100] if kind == "valid" else "expected to fail but passed"
            print(f"FAIL {kind}/{f.name}: {why}")

print(f"\n{counts['valid']} valid, {counts['invalid']} invalid samples, {failures} failure(s)")
sys.exit(1 if failures else 0)
