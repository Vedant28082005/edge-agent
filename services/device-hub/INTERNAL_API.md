# device-hub internal API

This file used to hold the S1 proposal. It was promoted to a contract and is now **normative in
[`contracts/openapi/device-hub-internal.yaml`](../../contracts/openapi/device-hub-internal.yaml)** (v1.0.0). Read that
file; nothing here overrides it. `tests/test_internal_contract.py` drives the router from that file (same operation
set, the 401/403/open behaviour per `x-scopes`, responses valid against its schemas).

Implementation notes that the contract does not spell out:

- **Caller auth is mandatory.** The hub reads `auth.verifier_dir` (default `/etc/edge-agent/secrets/hub-verifiers`,
  `gateway.sha256` required, `agent.sha256` optional) and refuses to start without it. `--mock --insecure-no-auth` is the
  only way to run without it (the flag is rejected without `--mock`) and logs a warning on every start. Scopes: gateway =
  every operation; agent = exactly two: `POST /mcp` and `GET /events`; `GET /health` is open. Unknown or missing secret:
  401; valid secret, wrong scope: 403.
- **`GET /events` for the agent** is filtered server-side from the caller's scope (`device_hub/events.py`
  `AGENT_ALLOWLIST`): `registry_changed`, `node_status`, `state` (opt-in), `action_settled`, `action_settle_timeout`.
  The filter applies to the subscription (live and replay via `Last-Event-ID` / `since`) and again on every frame
  (default-deny: a type not named in the allowlist, e.g. a future provisioning event, is never sent to the agent).
  Default for the agent is the allowlist minus `state`; a known type outside the allowlist in `types` is
  `403 forbidden_event_type`, an unknown type `400`. Frame format is the same for both scopes (`id`, `event`,
  `data` = `{seq, type, ts, data}`).
- **`_meta.call_id`** (optional, string, truncated to 64, non-strings ignored) is echoed verbatim as `call_id` on the
  `action_settled` / `action_settle_timeout` events of that call (each `moving` action of `run_scene` too). It plays no
  part in any decision.
- Registry change signal for the agent: `registry_changed` (in-process hook `Hub.bus.add_hook(fn)` or the SSE stream).
