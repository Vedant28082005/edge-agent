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

# --- OpenAPI: valid, and /admin/* is separate and admin-gated -----------------
import re

import yaml
from openapi_spec_validator import validate as validate_openapi

spec = yaml.safe_load((contracts / "openapi" / "gateway.yaml").read_text(encoding="utf-8"))
try:
    validate_openapi(spec)
    print("ok   openapi/gateway.yaml is a valid OpenAPI document")
except Exception as e:  # noqa: BLE001
    failures += 1
    print(f"FAIL openapi/gateway.yaml: {str(e)[:120]}")
for path, item in spec["paths"].items():
    for method, op in item.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        gated = op.get("x-required-role") == "admin"
        if path.startswith("/admin/") != gated:
            failures += 1
            print(f"FAIL openapi {method.upper()} {path}: /admin/* must be exactly the admin-gated operations")
        if path.startswith("/admin/") and "403" not in op["responses"]:
            failures += 1
            print(f"FAIL openapi {method.upper()} {path}: admin operation lacks a 403 response")
print("ok   openapi admin gating checked")

# --- docs: no devices/ topics, exactly 5 MCP tools -------------------------------
for doc in contracts.glob("*.md"):
    if re.search(r"home/v1/devices|[\s`]devices/\{", doc.read_text(encoding="utf-8")):
        failures += 1
        print(f"FAIL {doc.name} mentions a devices/ topic")
tools_md = (contracts / "mcp-tools.md").read_text(encoding="utf-8")
tools = [t for t in re.findall(r"^## (\w+)$", tools_md, re.M) if t != "Rules"]
if sorted(tools) != sorted(["list_devices", "get_state", "set_state", "run_scene", "read_sensor"]):
    failures += 1
    print(f"FAIL mcp-tools.md defines {tools}")
else:
    print("ok   mcp-tools.md defines exactly the 5 runtime tools")

print(f"\n{counts['valid']} valid, {counts['invalid']} invalid samples, {failures} failure(s)")
sys.exit(1 if failures else 0)
