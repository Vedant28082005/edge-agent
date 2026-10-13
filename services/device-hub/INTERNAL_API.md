# device-hub internal API

This file used to hold the S1 proposal. It was promoted to a contract and is now **normative in
[`contracts/openapi/device-hub-internal.yaml`](../../contracts/openapi/device-hub-internal.yaml)** (v1.0.0). Read that
file; nothing here overrides it. `tests/test_internal_contract.py` drives the router from that file (same operation
set, the 401/403/open behaviour per `x-scopes`, responses valid against its schemas).

Implementation notes that the contract does not spell out:

- **Caller auth is mandatory.** The hub reads `auth.verifier_dir` (default `/etc/edge-agent/secrets/hub-verifiers`,
  `gateway.sha256` required, `agent.sha256` optional) and refuses to start without it. `--mock --insecure-no-auth` is the
  only way to run without it (the flag is rejected without `--mock`) and logs a warning on every start. Scopes: gateway =
  every operation; agent = `POST /mcp` only; `GET /health` is open. Unknown or missing secret: 401; valid secret, wrong
  scope: 403.
- **Events beyond the contract's list.** `GET /events` also carries `action_settled` and `action_settle_timeout`
  (`{endpoint_id, capability, value, observed, elapsed_ms | settle_s, call_id?}`) and they are part of the default set.
  The contract's `/events` text does not list them yet, and the agent scope cannot read `/events`: see the S1 report.
- **`_meta`** on MCP calls: `confirmation_token`, `user_id` (forwarded, never trusted) and an optional `call_id`
  (correlation label echoed on the settle events; not in `mcp-tools.md`).
- Registry change signal for the agent: `registry_changed` (in-process hook `Hub.bus.add_hook(fn)` or the SSE stream).
