# MCP tools (device-hub), runtime only (v1)

The MCP server exposes **exactly five tools**. The list is fixed and never changes as devices come and go. There is **no** tool to add, remove, rename, rebind or re-provision a device, to change a risk level, to create/edit/delete scenes or users, or to touch MQTT credentials. All of that is `/admin/*` (see `openapi/gateway.yaml`) and is unreachable from the LLM. A review rule: any PR adding a sixth tool needs a contract version bump and owner sign-off.

Endpoint identity is always the permanent `endpoint_id` (`ep_` + 8 lowercase hex, e.g. `ep_3fa91c02`). Names/rooms are labels. Any device-supplied text in results is sanitized and length-limited.

## list_devices
Args: `room?: string`, `type?: string` (endpoint type enum).
Returns: `{"devices": [{"endpoint_id", "name", "room", "type", "online": bool, "capabilities": [{"id", "kind", "type", "min?", "max?", "values?", "unit?"}]}]}`. Excludes disabled endpoints. No live state.

## get_state
Args: exactly one of `endpoint_id: string` | `room: string`.
Returns: `{"states": [{"endpoint_id", "online": bool, "values": {"<cap>": <value>}, "ts": <unix s>}]}`. Offline endpoints return last known values with `online:false`.

## set_state
Args: `endpoint_id: string`, `capability: string`, `value: bool|number|string`, `confirmed?: boolean`.
Behavior: validated against the registry (endpoint exists, enabled, online, capability is an actuator, type and range ok). Sends a desired-state `cmd`, waits up to **3 s** for the `ack`, then verifies.
`risk: confirm` endpoints require `confirmed: true`, which the agent sets **only after the user confirms**; otherwise result is `{"ok": false, "error": "confirmation_required"}`.
Returns: `{"ok": bool, "endpoint_id", "capability", "value", "error?": code}`.
Error codes: `unknown_endpoint`, `disabled`, `offline`, `unknown_capability`, `not_actuator`, `bad_type`, `out_of_range`, `confirmation_required`, `ack_timeout`, `device_error`.

## run_scene
Args: `scene: string` (scene id or exact name).
Behavior: validates each action, runs them through the same executor as `set_state`. Offline or incapable endpoints are skipped and reported; nothing is queued. `risk: confirm` endpoints in the scene need the user's confirmation (the call returns them as `needs_confirmation` and the agent re-calls after user consent with the same scene; not a per-call arg).
Returns: `{"ok": bool, "scene", "results": [{"endpoint_id", "capability", "status": "ok|skipped_offline|skipped_no_capability|needs_confirmation|failed", "detail?"}]}`.

## read_sensor
Args: `endpoint_id: string`, `capability: string` (a sensor capability).
Returns: `{"endpoint_id", "capability", "value", "unit", "ts": <unix s>, "stale": bool}`. `stale` = older than 3x the announced `interval_s` or endpoint offline.

## Rules

- The agent's constrained-decoding schema is generated from the live registry; the tools above remain the only side-effect surface.
- The LLM may *propose* a scene/automation as JSON in its reply; saving it is a gateway call by a user, not a tool.
