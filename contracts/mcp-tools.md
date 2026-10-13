# MCP tools (device-hub), runtime only (v4)

The MCP server exposes **exactly five tools**. The list is fixed and never changes as devices come and go. There is **no** tool to add, remove, rename, rebind or re-provision a device, to change a risk level, to create/edit/delete scenes or users, or to touch MQTT credentials. All of that is `/admin/*` (see `openapi/gateway.yaml`) and is unreachable from the LLM. A review rule: any PR adding a sixth tool needs a contract version bump and owner sign-off.

Endpoint identity is always the permanent `endpoint_id` (`ep_` + 8 lowercase hex, e.g. `ep_3fa91c02`). Names/rooms are labels. Any device-supplied text in results is sanitized and length-limited.

Version 4 change: see the Contract changelog in `mqtt.md` (S0c). Items are described inline below.
Version 3 change: motion capabilities (those announcing `settle_s`) return `status: "moving"` and are verified asynchronously (see `set_state`); `run_scene` per-action statuses are `done|moving|...`.
Version 2 change: the boolean `confirmed` argument is gone; confirmation is a `confirmation_token` (below).

## Confirmation tokens (`risk: confirm` endpoints)

- A token is **opaque** (256-bit random, base64url), **single-use**, bound to **user + exact action + short TTL (60 s)**, and **minted by the gateway** only when the user approves in the UI (`POST /confirmations`, see `openapi/gateway.yaml`). The agent cannot mint one.
- **It is not part of any tool's `inputSchema`.** The LLM never sees it, cannot generate it and cannot pass it. The agent **executor** attaches it out-of-band in the MCP call's `_meta` field: `{"name": "set_state", "arguments": {...}, "_meta": {"confirmation_token": "<token>"}}`. The tool schema list sent to the model contains no confirmation field; any `confirmation_token` found in the model's own arguments is stripped and logged by the executor.
- **Binding.** The token is bound to the requesting `user_id` and to `action_hash` (defined precisely below). The gateway stores only `SHA-256(token)`, the user, the action hash and the expiry.
- **Validation** is done by device-hub when it runs the action: it calls the gateway's internal consume endpoint (`POST /internal/confirmations/consume`: loopback or unix socket only, shared-secret authenticated; specified in the clearly marked internal section of `openapi/gateway.yaml`) with `{token, user_id, action_hash}`. The gateway validates and marks it used in one atomic operation, so replay and races fail. A token presented for a different action (`hash_mismatch`) is **burned** (fail closed); a different user's attempt returns `unknown` and does not burn it. Any denial (`expired`, `used`, `hash_mismatch`, `unknown`) is treated as **no token**: the tool returns `confirmation_required` / `needs_confirmation`. Only the gateway can mint tokens; the agent holds neither the service secret nor any mint or consume access.
- A token for `set_state` cannot confirm a `run_scene` and vice versa. Guests can never obtain one. Automations never touch `risk: confirm` endpoints at all (they only notify; there is no admin opt-in, see ARCHITECTURE 4.7).

## Canonical action hash

`action_hash` = lowercase hex SHA-256 of the UTF-8 bytes of the canonical JSON of the action object below. Canonical JSON is **RFC 8785 (JCS)**: no whitespace, object keys sorted by UTF-16 code unit, minimal string escaping, numbers in ECMAScript shortest form (`3`, not `3.0`; `0.5`; no `+`, no exponent for ordinary values). The object has **exactly these fields, no others** (a missing or extra field changes the hash):

`set_state`:
```json
{"capability": "power", "endpoint_id": "ep_3fa91c02", "type": "set_state", "value": true}
```

`run_scene` (hash covers the scene id and the **`risk: confirm` actions only**, as resolved at that moment, so a scene that later resolves to different risky targets needs a new approval):
```json
{"actions": [{"capability": "lock", "endpoint_id": "ep_1b2c3d4e", "value": "unlocked"}], "scene_id": "sc_9a8b7c6d", "type": "run_scene"}
```
- `scene_id` is the scene's permanent id, never its display name.
- `actions` is sorted ascending by `endpoint_id`, then `capability` (plain string comparison). Each entry has exactly `endpoint_id`, `capability`, `value`.
- Offline or incapable risky actions are still listed (they are part of what the user approved); they are skipped at run time as usual.

Both the gateway (when minting) and device-hub (when consuming) must compute the hash from their own view of the action; they never trust a hash supplied by the agent or the client. Test vectors: `samples/confirmation/hash-vectors.json` (checked by `tools\contract-test.ps1`).

## list_devices
Args: `room?: string`, `type?: string` (endpoint type enum).
Returns: `{"devices": [{"endpoint_id", "name", "room", "type", "online": bool, "risk": "normal|confirm", "capabilities": [{"id", "kind", "type", "min?", "max?", "values?", "unit?"}]}]}`. Excludes disabled endpoints. No live state.

## get_state
Args: exactly one of `endpoint_id: string` | `room: string`.
Returns: `{"states": [{"endpoint_id", "online": bool, "values": {"<cap>": <value>}, "ts": <unix s>}]}`. Offline endpoints return last known values with `online:false`.

## set_state
Args: `endpoint_id: string`, `capability: string`, `value: bool|number|string`. (Nothing else.)
Behavior: validated against the registry (endpoint exists, enabled, online, capability is an actuator, type and range ok). Sends a desired-state `cmd`, waits up to **3 s** for the `ack`, then verifies.

**Ack semantics.** The node's `ack` means "accepted, motion started" and its `values` echo the accepted target, not the position reached. Actual progress is reported only in `state`. The ack timeout stays 3 s for all capabilities.
- **No `settle_s`** (instant capabilities such as `power`): the hub verifies the state against the target right after the ack and returns `status: "done"`.
- **With `settle_s`** (motion, e.g. `position`): the tool returns `status: "moving"` as soon as the ack arrives. The hub then watches `state` and checks that it converges to the target within `settle_s` seconds. Tolerance: **+/-2** for `position` (other motion capabilities: exact match for enum/bool, +/-2 for int percentages unless a later contract says otherwise). Success emits the `action_settled` event, failure `action_settle_timeout` (see `agent-events.md`). The tool call itself does not block for `settle_s`.
If the endpoint is `risk: confirm` and no valid `confirmation_token` is attached out-of-band, nothing is sent and the result is `{"ok": false, "error": "confirmation_required"}`.
Returns: `{"ok": bool, "status?": "done|moving", "endpoint_id", "capability", "value", "error?": code}`. `status` is present when `ok` is true. `ok:true, status:"moving"` means accepted and started, not yet reached.
Error codes: `unknown_endpoint`, `disabled`, `offline`, `unknown_capability`, `not_actuator`, `bad_type`, `out_of_range`, `confirmation_required`, `ack_timeout`, `device_error`.

## run_scene
Args: `scene: string` (scene id or exact name). (Nothing else.)
Behavior: resolves the scene's targets (selectors included) and validates every action, then:
1. If **any** action targets a `risk: confirm` endpoint and there is **no valid `confirmation_token`** (attached out-of-band) covering exactly those actions, **nothing is executed at all**, including the non-risky actions. The result is `needs_confirmation`, listing the risky actions so the UI can ask the user.
2. Otherwise all actions run through the same executor as `set_state`. Offline or incapable endpoints are skipped and reported; nothing is queued. Actions on capabilities with `settle_s` report `moving` and are verified asynchronously per action (`action_settled` / `action_settle_timeout`).

Returns when blocked: `{"ok": false, "executed": false, "status": "needs_confirmation", "needs_confirmation": [{"endpoint_id", "capability", "value"}]}`.
Returns when run: `{"ok": bool, "executed": true, "scene", "results": [{"endpoint_id", "capability", "status": "done|moving|skipped_offline|skipped_no_capability|failed", "detail?"}]}`.

## read_sensor
Args: `endpoint_id: string`, `capability: string` (a sensor capability).
Returns: `{"endpoint_id", "capability", "value", "unit", "ts": <unix s>, "stale": bool}`. `stale` = older than 3x the announced `interval_s` or endpoint offline.

## Rules

- The agent's constrained-decoding schema is generated from the live registry; the tools above remain the only side-effect surface.
- The LLM may *propose* a scene/automation as JSON in its reply; saving it is a gateway call by a user, not a tool.
- A `needs_confirmation` / `confirmation_required` result makes the agent emit a `confirmation_request` event (see `agent-events.md`); it never retries with a guessed token.
