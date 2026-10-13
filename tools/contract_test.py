"""Validate every contract sample against its JSON Schema (announce, state, cmd, ack, perception-event),
then check the OpenAPI file and the MCP tool list."""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

contracts = Path(sys.argv[1])

# schema file -> sample folder (announce samples predate the others and live at samples/{valid,invalid})
SUITES = {
    "announce": ("announce.schema.json", contracts / "samples"),
    "state": ("state.schema.json", contracts / "samples" / "state"),
    "cmd": ("cmd.schema.json", contracts / "samples" / "cmd"),
    "ack": ("ack.schema.json", contracts / "samples" / "ack"),
    "perception-event": ("perception-event.schema.json", contracts / "samples" / "perception-event"),
}

failures = 0
counts = {"valid": 0, "invalid": 0}
for name, (schema_file, base) in SUITES.items():
    schema = json.loads((contracts / schema_file).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for kind in ("valid", "invalid"):
        files = sorted((base / kind).glob("*.json"))
        if not files:
            print(f"FAIL: no samples for {name} in {kind}/")
            failures += 1
        for f in files:
            counts[kind] += 1
            errors = list(validator.iter_errors(json.loads(f.read_text(encoding="utf-8"))))
            ok = (not errors) if kind == "valid" else bool(errors)
            label = f"{name}/{kind}/{f.name}"
            if ok:
                note = "" if kind == "valid" else f"  (rejected: {errors[0].message[:60]})"
                print(f"ok   {label}{note}")
            else:
                failures += 1
                why = "expected to pass but failed: " + errors[0].message[:100] if kind == "valid" else "expected to fail but passed"
                print(f"FAIL {label}: {why}")

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

# --- OpenAPI component samples (internal consume endpoint) -------------------------
OPENAPI_SUITES = {
    "consume-request": "ConsumeRequest",
    "consume-response": "ConsumeResponse",
}
spec_for_refs = dict(spec)
# ConsumeResponse = granted or denied
spec["components"]["schemas"]["ConsumeResponse"] = {
    "oneOf": [{"$ref": "#/components/schemas/ConsumeGranted"}, {"$ref": "#/components/schemas/ConsumeDenied"}]}
for folder, comp in OPENAPI_SUITES.items():
    validator = Draft202012Validator({"$ref": f"#/components/schemas/{comp}", "components": spec["components"]})
    for kind in ("valid", "invalid"):
        files = sorted((contracts / "samples" / folder / kind).glob("*.json"))
        if not files:
            failures += 1
            print(f"FAIL: no samples for {folder} in {kind}/")
        for f in files:
            counts[kind] += 1
            errors = list(validator.iter_errors(json.loads(f.read_text(encoding="utf-8"))))
            ok = (not errors) if kind == "valid" else bool(errors)
            if ok:
                print(f"ok   {folder}/{kind}/{f.name}")
            else:
                failures += 1
                print(f"FAIL {folder}/{kind}/{f.name}: " + ("expected to pass but failed: " + errors[0].message[:100] if kind == "valid" else "expected to fail but passed"))

# internal section: only service-secret auth, never user JWT, never /admin
for path, item in spec["paths"].items():
    for method, op in item.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        is_internal = path.startswith("/internal/")
        sec = [list(x) for x in op.get("security", [])]
        if is_internal:
            callers = op.get("x-internal-callers", [])
            want = [[c.replace("-", " ").title().replace(" ", "")[0].lower() + c.replace("-", " ").title().replace(" ", "")[1:] + "Secret"] for c in callers]
            if not callers or sec != want or "internal" not in op.get("tags", []):
                failures += 1
                print(f"FAIL openapi {method.upper()} {path}: internal ops must be tagged internal and use exactly one <service>Secret scheme per x-internal-callers entry")
        if not is_internal and any(k.endswith("Secret") for x in sec for k in x):
            failures += 1
            print(f"FAIL openapi {method.upper()} {path}: service secrets are for /internal/* only")
        if is_internal and "agent" in op.get("x-internal-callers", []):
            failures += 1
            print(f"FAIL openapi {method.upper()} {path}: the agent must not be an internal caller")
print("ok   openapi internal section checked")

# --- device-hub internal API: valid OpenAPI, mandatory auth, agent scope has no admin operation ----------
RUNTIME_TOOLS = ["list_devices", "get_state", "set_state", "run_scene", "read_sensor"]
hub = yaml.safe_load((contracts / "openapi" / "device-hub-internal.yaml").read_text(encoding="utf-8"))
try:
    validate_openapi(hub)
    print("ok   openapi/device-hub-internal.yaml is a valid OpenAPI document")
except Exception as e:  # noqa: BLE001
    failures += 1
    print(f"FAIL openapi/device-hub-internal.yaml: {str(e)[:120]}")
_f0 = failures
SCHEME = {"gateway": "gatewaySecret", "agent": "agentSecret"}
agent_ops = []
for path, item in hub["paths"].items():
    for method, op in item.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        label = f"{method.upper()} {path}"
        scopes = op.get("x-scopes")
        sec = op.get("security", hub.get("security", []))
        names = sorted(k for x in sec for k in x)
        if scopes == ["open"]:
            # the only unauthenticated operation is the liveness probe
            if path != "/health" or sec != []:
                failures += 1
                print(f"FAIL hub-internal {label}: only GET /health may be unauthenticated")
            continue
        if not scopes or "gateway" not in scopes:
            failures += 1
            print(f"FAIL hub-internal {label}: x-scopes must be set and include gateway (the gateway secret allows everything)")
            continue
        if names != sorted(SCHEME[x] for x in scopes):
            failures += 1
            print(f"FAIL hub-internal {label}: security {names} does not match x-scopes {scopes}")
        if "agent" in scopes:
            agent_ops.append((path, op))
admin_like = lambda p: p.startswith(("/admin", "/scenes", "/devices", "/events", "/registry", "/rooms", "/mock"))
for path, op in agent_ops:
    if admin_like(path) or path != "/mcp" or sorted(op.get("x-mcp-tools", [])) != sorted(RUNTIME_TOOLS):
        failures += 1
        print(f"FAIL hub-internal agent scope may only call POST /mcp exposing exactly the 5 runtime tools, found {path}")
if not agent_ops:
    failures += 1
    print("FAIL hub-internal: agent scope has no operation at all (it must reach /mcp)")
if "agentSecret" not in hub["components"]["securitySchemes"] or "gatewaySecret" not in hub["components"]["securitySchemes"]:
    failures += 1
    print("FAIL hub-internal: gatewaySecret and agentSecret schemes are required")
if failures == _f0:
    print("ok   hub-internal auth mandatory; agent scope = runtime tools only")

# --- canonical action hash vectors -------------------------------------------------
import copy
import hashlib


def _norm(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def canonical_action(action):
    """Reference canonicalizer (RFC 8785 for the value types used by actions)."""
    a = _norm(copy.deepcopy(action))
    if a["type"] == "run_scene":
        a["actions"] = sorted(a["actions"], key=lambda x: (x["endpoint_id"], x["capability"]))
    return json.dumps(a, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


vec = json.loads((contracts / "samples" / "confirmation" / "hash-vectors.json").read_text(encoding="utf-8"))
for v in vec["vectors"]:
    c = canonical_action(v["input"])
    good = c == v["canonical"] and hashlib.sha256(c.encode("utf-8")).hexdigest() == v["sha256"]
    if good:
        print(f"ok   confirmation hash vector: {v['name']}")
    else:
        failures += 1
        print(f"FAIL confirmation hash vector: {v['name']}")

# --- consume semantics (reference implementation of the atomic rule in gateway.yaml) -----
class RefTokenStore:
    """One token. consume() mirrors: UPDATE ... SET used=1 WHERE user_id=? AND used=0 AND expires_at>? RETURNING hash."""

    def __init__(self, token):
        self.token = token
        self.used = False

    def consume(self, at, user, action_hash):
        t = self.token
        if t is not None and t["user"] == user and not self.used and at < t["ttl"]:
            self.used = True  # burned whatever the hash turns out to be (fail closed)
            return "granted" if t["hash"] == action_hash else "hash_mismatch"
        if t is None or t["user"] != user:
            return "unknown"
        return "used" if self.used else "expired"


scen = json.loads((contracts / "samples" / "confirmation" / "consume-scenarios.json").read_text(encoding="utf-8"))
for sc in scen["scenarios"]:
    store = RefTokenStore(sc["token"])
    got = [store.consume(c["at"], c["user"], c["hash"]) for c in sc["calls"]]
    want = [c["expect"] for c in sc["calls"]]
    if got == want:
        print(f"ok   consume scenario: {sc['name']}")
    else:
        failures += 1
        print(f"FAIL consume scenario: {sc['name']}: got {got}, want {want}")

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
