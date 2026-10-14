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
    "scene-action": "SceneAction",
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
FORBIDDEN_AGENT_EVENTS = {"scenes_changed", "pending_node", "provision", "provisioning", "admin", "audit", "confirmation",
                          "credential", "secret", "token", "wifi", "bundle", "session"}
agent_set = sorted((p, m) for p, op in agent_ops for m in [next(k for k, v in hub["paths"][p].items() if v is op)])
if agent_set != [("/events", "get"), ("/mcp", "post")]:
    failures += 1
    print(f"FAIL hub-internal agent scope must contain exactly POST /mcp and GET /events, found {agent_set}")
for path, op in agent_ops:
    if path == "/mcp" and sorted(op.get("x-mcp-tools", [])) != sorted(RUNTIME_TOOLS):
        failures += 1
        print("FAIL hub-internal POST /mcp must expose exactly the 5 runtime tools")
    if path == "/events":
        allow = op.get("x-agent-event-allowlist")
        gw_only = op.get("x-gateway-only-event-types", [])
        envelope_types = hub["components"]["schemas"]["EventEnvelope"]["properties"]["type"]["enum"]
        if not allow or not isinstance(allow, list):
            failures += 1
            print("FAIL hub-internal GET /events must document x-agent-event-allowlist")
        else:
            bad = [t for t in allow if t in FORBIDDEN_AGENT_EVENTS or any(w in t for w in ("admin", "provision", "credential", "secret", "token", "wifi", "bundle"))]
            if bad:
                failures += 1
                print(f"FAIL hub-internal agent event allowlist contains admin/provisioning types: {bad}")
            if any(t in gw_only for t in allow) or set(allow) & set(gw_only):
                failures += 1
                print("FAIL hub-internal agent allowlist overlaps x-gateway-only-event-types")
            if not set(allow) <= set(envelope_types):
                failures += 1
                print(f"FAIL hub-internal allowlist types not in EventEnvelope.type: {sorted(set(allow) - set(envelope_types))}")
            # every allowlisted type must have its payload documented in the operation description
            undoc = [t for t in allow if f"* `{t}`" not in op.get("description", "")]
            if undoc:
                failures += 1
                print(f"FAIL hub-internal GET /events does not document the payload of {undoc}")
            for t in ("action_settled", "action_settle_timeout"):
                if t not in allow:
                    failures += 1
                    print(f"FAIL hub-internal allowlist must contain {t}")
for path, item in hub["paths"].items():
    for method, op in item.items():
        if method in ("get", "post", "put", "patch", "delete") and "agent" in (op.get("x-scopes") or []) and (path, method) not in (("/events", "get"), ("/mcp", "post")):
            failures += 1
            print(f"FAIL hub-internal agent scope may only call POST /mcp and GET /events, found {method.upper()} {path}")
if not agent_ops:
    failures += 1
    print("FAIL hub-internal: agent scope has no operation at all (it must reach /mcp)")
if "agentSecret" not in hub["components"]["securitySchemes"] or "gatewaySecret" not in hub["components"]["securitySchemes"]:
    failures += 1
    print("FAIL hub-internal: gatewaySecret and agentSecret schemes are required")
if failures == _f0:
    print("ok   hub-internal auth mandatory; agent scope = POST /mcp + allowlisted GET /events only")

# --- agent internal API: valid OpenAPI, auth mandatory on EVERY operation, own secret ------------------
agent_api = yaml.safe_load((contracts / "openapi" / "agent-internal.yaml").read_text(encoding="utf-8"))
try:
    validate_openapi(agent_api)
    print("ok   openapi/agent-internal.yaml is a valid OpenAPI document")
except Exception as e:  # noqa: BLE001
    failures += 1
    print(f"FAIL openapi/agent-internal.yaml: {str(e)[:120]}")
_f1 = failures
if list(agent_api["components"]["securitySchemes"]) != ["agentApiSecret"]:
    failures += 1
    print("FAIL agent-internal: exactly one security scheme (agentApiSecret) is expected, distinct from hub secrets")
for path, item in agent_api["paths"].items():
    for method, op in item.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        sec = op.get("security", agent_api.get("security", []))
        if sec != [{"agentApiSecret": []}]:
            failures += 1
            print(f"FAIL agent-internal {method.upper()} {path}: every operation must require agentApiSecret")
        if "401" not in op["responses"]:
            failures += 1
            print(f"FAIL agent-internal {method.upper()} {path}: missing 401 response")
for need in ("/v1/chat", "/v1/confirmations/{request_id}", "/v1/events", "/v1/memory", "/v1/memory/{id}", "/v1/health"):
    if need not in agent_api["paths"]:
        failures += 1
        print(f"FAIL agent-internal: missing path {need}")
if failures == _f1:
    print("ok   agent-internal auth mandatory on every operation")
cd_validator = Draft202012Validator({"$ref": "#/components/schemas/ConfirmationDecision", "components": agent_api["components"]})
_h = "a" * 64
_act = {"type": "run_scene", "scene_id": "sc_9a8b7c6d", "actions": [{"endpoint_id": "ep_1b2c3d4e", "capability": "lock", "value": "unlocked"}]}
for name, body, want_ok in [
    ("approved with token", {"decision": "approved", "user_id": "u1", "action": _act, "action_hash": _h, "confirmation_token": "A" * 43}, True),
    ("declined without token", {"decision": "declined", "user_id": "u1", "action": _act, "action_hash": _h}, True),
    ("expired without token", {"decision": "expired", "user_id": "u1", "action": _act, "action_hash": _h}, True),
    ("approved without token", {"decision": "approved", "user_id": "u1", "action": _act, "action_hash": _h}, False),
    ("declined with token", {"decision": "declined", "user_id": "u1", "action": _act, "action_hash": _h, "confirmation_token": "A" * 43}, False),
    ("approved without action", {"decision": "approved", "user_id": "u1", "action_hash": _h, "confirmation_token": "A" * 43}, False),
]:
    ok = (not list(cd_validator.iter_errors(body))) == want_ok
    if not ok:
        failures += 1
    print(("ok   " if ok else "FAIL ") + f"agent-internal confirmation decision: {name}")

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
