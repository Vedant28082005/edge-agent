# MCP tools (device-hub), runtime only (v2)

The MCP server exposes **exactly five tools**. The list is fixed and never changes as devices come and go. There is **no** tool to add, remove, rename, rebind or re-provision a device, to change a risk level, to create/edit/delete scenes or users, or to touch MQTT credentials. All of that is `/admin/*` (see `openapi/gateway.yaml`) and is unreachable from the LLM. A review rule: any PR adding a sixth tool needs a contract version bump and owner sign-off.

Endpoint identity is always the permanent `endpoint_id` (`ep_` + 8 lowercase hex, e.g. `ep_3fa91c02`). Names/rooms are labels. Any device-supplied text in results is sanitized and length-limited.

Version 2 change: the boolean `confirmed` argument is gone; confirmation is a `confirmation_token` (below).

## Confirmation tokens (`risk: confirm` endpoints)

- A token is **opaque** (256-bit random, base64url), **single-use**, bound to **user + exact action + short TTL (60 s)**, and **minted by the gateway** only when the user approves in the UI (`POST /confirmations`, see `openapi/gateway.yaml`). The agent cannot mint one.
- **It is not part of any tool's `inputSchema`.** The LLM never sees it, cannot generate it and cannot pass it. The agent **executor** attaches it out-of-band in the MCP call's `_meta` field: `{"name": "set_state", "arguments": {...}, "_meta": {"confirmation_token": "<token>"}}`. The tool schema list sent to the model contains no confirmation field; any `confirmation_token` found in the model's own arguments is stripped and logged by the executor.
- **Binding.** `action_hash = sha256(canonical_json({tool, args}))`. For `set_state` the args are `endpoint_id, capability, value`. For `run_scene` they are the scene id plus the sorted list of `risk: confirm` `(endpoint_id, capability, value)` actions the scene resolves to at that moment. The token is also bound to the requesting `user_id`.
- **Validation** is done by device-hub when it runs the action: it calls the gateway's internal consume endpoint (`POST /internal/confirmations/consume`, localhost/service-to-service, not part of the public OpenAPI) with `{token, user_id, action_hash}`. The gateway atomically marks it used, so replay fails. Expired, used, unknown, wrong-user or wrong-action tokens are all treated as **no token**.
- A token for `set_state` cannot confirm a `run_scene` and vice versa. Automations and guests can never obtain one for `risk: confirm` endpoints unless the admin opted that endpoint in (see ARCHITECTURE 4.7).

## list_devices
Args: `room?: string`, `type?: string` (endpoint type enum).
Returns: `{"devices": [{"endpoint_id", "name", "room", "type", "online": bool, "risk": "normal|confirm", "capabilities": [{"id", "kind", "type", "min?", "max?", "values?", "unit?"}]}]}`. Excludes disabled endpoints. No live state.

## get_state
Args: exactly one of `endpoint_id: string` | `room: string`.
Returns: `{"states": [{"endpoint_id", "online": bool, "values": {"<cap>": <value>}, "ts": <unix s>}]}`. Offline endpoints return last known values with `online:false`.

## set_state
Args: `endpoint_id: string`, `capability: string`, `value: bool|number|string`. (Nothing else.)
Behavior: validated against the registry (endpoint exists, enabled, online, capability is an actuator, type and range ok). Sends a desired-state `cmd`, waits up to **3 s** for the `ack`, then verifies.
If the endpoint is `risk: confirm` and no valid `confirmation_token` is attached out-of-band, nothing is sent and the result is `{"ok": false, "error": "confirmation_required"}`.
Returns: `{"ok": bool, "endpoint_id", "capability", "value", "error?": code}`.
Error codes: `unknown_endpoint`, `disabled`, `offline`, `unknown_capability`, `not_actuator`, `bad_type`, `out_of_range`, `confirmation_required`, `ack_timeout`, `device_error`.

## run_scene
Args: `scene: string` (scene id or exact name). (Nothing else.)
Behavior: resolves the scene's targets (selectors included) and validates every action, then:
1. If **any** action targets a `risk: confirm` endpoint and there is **no valid `confirmation_token`** (attached out-of-band) covering exactly those actions, **nothing is executed at all**, including the non-risky actions. The result is `needs_confirmation`, listing the risky actions so the UI can ask the user.
2. Otherwise all actions run through the same executor as `set_state`. Offline or incapable endpoints are skipped and reported; nothing is queued.

Returns when blocked: `{"ok": false, "executed": false, "status": "needs_confirmation", "needs_confirmation": [{"endpoint_id", "capability", "value"}]}`.
Returns when run: `{"ok": bool, "executed": true, "scene", "results": [{"endpoint_id", "capability", "status": "ok|skipped_offline|skipped_no_capability|failed", "detail?"}]}`.

## read_sensor
Args: `endpoint_id: string`, `capability: string` (a sensor capability).
Returns: `{"endpoint_id", "capability", "value", "unit", "ts": <unix s>, "stale": bool}`. `stale` = older than 3x the announced `interval_s` or endpoint offline.

## Rules

- The agent's constrained-decoding schema is generated from the live registry; the tools above remain the only side-effect surface.
- The LLM may *propose* a scene/automation as JSON in its reply; saving it is a gateway call by a user, not a tool.
- A `needs_confirmation` / `confirmation_required` result makes the agent emit a `confirmation_request` event (see `agent-events.md`); it never retries with a guessed token.
