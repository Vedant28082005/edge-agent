# device-hub internal API (PROPOSAL, v0.1: needs owner approval)

The contracts define the MCP tools (`contracts/mcp-tools.md`), the MQTT topics and the gateway's public `/admin/*`, but **not** how the gateway reaches device-hub. This file proposes that interface. It is implemented in `device_hub/api.py` and covered by `tests/test_api.py`. Nothing here is in `contracts/`; if you approve it, it should move there (and get a version header).

- Listener: `http://127.0.0.1:8100` (loopback only; never exposed through `tailscale serve`). JSON in and out, UTF-8, request bodies max 64 KiB.
- Errors: `{"error": "<code>", "message": "<text>"}` with the HTTP status noted per endpoint. `400` bad request, `404` unknown id, `409` conflict / needs confirmation, `413` too large, `502/503/504` MQTT/dynsec failure, `500` internal (no details leaked).
- `X-Actor: <user id>` (optional, default `gateway`) is written to the audit log for every mutating call. The gateway has already authorised the caller by role; **the hub does no user or role checks**.
- **Caller authentication (optional, recommended on the Pi):** `X-Service-Secret: <secret>` checked against `<service>.sha256` verifier files in `auth.verifier_dir` (hex SHA-256 of the secret). Scopes: `gateway` = everything except `/mcp`; `agent` = `/mcp`, `GET /events`, `GET /registry/version`, `GET /health` only (so the agent can drive the five tools and follow registry changes but cannot reach any admin route). `/health` is open. With `auth.verifier_dir` unset (dev) there is no caller auth and a warning is logged. This follows CLAUDE.md rule 10 (one secret per calling service, scoped); it needs `tools/setup-pi-brain.sh` to create `gateway.secret` and `agent.secret` plus verifiers readable by the hub user (today's script writes verifiers readable only by the gateway user for the opposite direction).
- Secrets: confirmation tokens and Wi-Fi passwords only ever travel in request/response **bodies**, never in URLs; the hub never logs bodies or tokens (tested).

## Provisioning and nodes (gateway: `/admin/*`, role admin)

| Method / path | Body / query | Success | Notes |
|---|---|---|---|
| `POST /admin/provision-sessions` | `{node_id, wifi:{ssid,password}}` | `201 ProvisionSession` (`session_id`, `expires_at`, `bundle{v:1, wifi, mqtt{host,port,username,password}, ntp_host, ca_pem}`) | Creates the node's MQTT user + role via dynsec (MQTT API). The gateway resolves `wifi_profile` before calling; the hub takes inline `wifi` only. Replaces any earlier session for that node (credentials rotate). `409 node_online` if the node is connected. The bundle is returned **once**; the MQTT and Wi-Fi passwords are not stored. TTL 10 min. |
| `GET /admin/provision-sessions?active=1` | | `200 [{session_id,node_id,state,created,expires_at}]` | `state`: pending / connected / expired / cancelled. No secrets. |
| `GET /admin/nodes` · `GET /admin/nodes/{node_id}` | | `200 Node` | `status`: `pending` (announced, nothing approved yet) / `online` / `offline`. Extras: `pending_endpoints` (announced but not approved, with capabilities), `online`, `provisioned`, `first_seen`, `last_seen`. |
| `POST /admin/nodes/{node_id}/approve` | `{endpoints:[{local_id,name,room,aliases?,risk?}]}` | `200 [Endpoint]` | Assigns `ep_<8 hex>`. All-or-nothing; names are unique per room (`409 name_conflict`). May be called again for endpoints a node adds later. |
| `POST /admin/nodes/{node_id}/identify` | `{duration_s?}` (1-60) | `202 {sent, acked}` | `409 offline`. |
| `POST /admin/nodes/{node_id}/unprovision?force=` | | `202 {sent, acked}` | Sends `cmd: unprovision`, then deletes the node's MQTT user/role. Registry rows stay (ids survive a re-provision). Offline node: `409` unless `force=true` (then only the credentials are revoked). |
| `DELETE /admin/nodes/{node_id}?confirm=&force=` | | `409` usage (no `confirm`), `204` forgotten | Usage body: `{error:"confirmation_required", scenes, automations, scene_list:[{scene_id,name}], message:"used by N scenes, M automations"}`. With `confirm=true`: delete MQTT user + role (dynsec), clear retained topics, soft-delete rows, flag dependent scenes broken. `force=true`: if the broker/dynsec is unreachable still soft-delete; the revocation is retried every 15 s until it succeeds. Without `force` a dynsec failure aborts and changes nothing (`502/503/504`). |
| `PATCH /admin/endpoints/{endpoint_id}` | `{name?,room?,aliases?,risk?,enabled?}` | `200 Endpoint` | Same body as the gateway OpenAPI; unknown fields `400`. |
| `POST /admin/endpoints/{endpoint_id}/rebind` | `{node_id, local_id}` | `200 Endpoint` | Same `type` required (`409 type_mismatch`); target must not be bound (`409 already_bound`). The id never changes. |
| `GET /admin/audit?limit=&action=` | | `200 [{id,ts,actor,action,target,detail,result}]` | `detail` never contains secrets. |
| `GET /admin/rejections` | | `200 [{node_id,reason,ts}]` | Last 100 rejected announces (also logged). |

## Devices and scenes (gateway user-facing routes)

| Method / path | Body | Success | Notes |
|---|---|---|---|
| `GET /devices?room=&type=` · `GET /devices/{id}` | | `200 [Endpoint]` / `Endpoint` | All approved endpoints including disabled ones (`enabled`), with `state`, `risk`, `online`. The gateway filters by role and room. |
| `POST /devices/{id}/set` | `{capability, value, user_id?, confirmation_token?}` | `200 {ok, endpoint_id, capability, value, error?, detail?, verified?}` | Same code path as MCP `set_state`. HTTP 200 even for `ok:false`; `404` for an unknown endpoint. The token is consumed via the gateway's internal endpoint. |
| `POST /devices/{id}/action` | `{capability, value}` | `200 {action, action_hash, risk}` | The canonical `set_state` action and its hash, so the gateway computes the same hash it mints for. |
| `GET /scenes` · `GET /scenes/{id}` | | `200 [Scene]` | Scene = OpenAPI `Scene` + `broken_reasons` + `rooms` (rooms touched; `*` = selector without room, for member room checks). |
| `POST /scenes` | `{name, actions, owner}` | `201 Scene` | Validated (targets, capability, value). `409 name_conflict`. |
| `PUT /scenes/{id}` | `{name?, actions?}` | `200 Scene` | Re-validated; clears `broken` if repaired. |
| `DELETE /scenes/{id}` | | `204` | Soft delete. |
| `POST /scenes/{id}/run` | `{user_id?, confirmation_token?}` | `200` run result · `409` `needs_confirmation` / `scene_broken` · `404` | Run result as `SceneRunResult`. `409` bodies carry `executed:false`. |
| `GET /scenes/{id}/confirmation` | | `200 {action, action_hash, needs_confirmation}` | The risky actions as resolved **now** and the `run_scene` hash. The gateway uses this to mint a scene token; it must not compute scene hashes from its own scene copy. |

## Registry change stream (for the agent's prompt and constrained-decoding schema)

- In-process: `Hub.bus.add_hook(fn)` (synchronous, must not block).
- Subscribable: `GET /events?types=registry_changed,scenes_changed,node_status,pending_node,state` (SSE; default is every type except `state`). `Last-Event-ID` (or `?since=`) replays from a 1000-event ring buffer. A `hello` event first carries `{version, last_event_seq}`. If you reconnect with a stale id, compare `GET /registry/version` and reload `list_devices`.
- `registry_changed` data: `{version, reason: approve|patch|rebind|forget|unprovision|announce_changed, endpoint_ids}`. This is the signal to rebuild the device list and the schema; it fires only for changes that matter to those (not for online/offline or sensor values).
- Other: `scenes_changed {reason, scene_ids}`, `node_status {node_id, online}`, `pending_node {node_id, local_ids}`, `state {endpoint_id, values, ts}` (opt-in).
- `GET /registry/version` → `{version, last_event_seq}`; `GET /rooms`; `GET /health` → `{ok, mqtt_connected, registry_version, nodes, nodes_online, pending_nodes, pending_acks, announce_rejected}`.

## MCP

`POST /mcp` (streamable HTTP, stateless, JSON responses) and `python -m device_hub --mcp-stdio`. Exactly the five tools of `contracts/mcp-tools.md`. The executor attaches `_meta: {"confirmation_token": "...", "user_id": "..."}` out of band. **`_meta.user_id` is an addition to the contract** (the consume call needs the user the token is bound to): see the decisions list in the S1 report. Host-header allow-list (DNS-rebinding protection) is limited to loopback.

## Mock mode only

`--mock` adds `POST /mock/mint {user_id, action}` → `{confirmation_token, action_hash}`, standing in for the gateway's `POST /confirmations` so the confirmation flow can be exercised without a gateway.
