# MQTT contract (v6)

Broker: Mosquitto on `pi-brain`, **TLS on 8883** (1883 disabled), dynamic-security plugin, anonymous access off. Prefix for all topics: `home/v1/`. Payloads: JSON, UTF-8, max 16 KiB.

## Node connection rules

- MQTT `client_id` == MQTT `username` == `node_id`. Always all three equal.
- **Clean session** (`clean_session=true` / MQTT 5 clean start with session expiry 0). The `cmd` topic is never retained, so there are no queued old commands to deliver after a reconnect; a persistent session would add nothing but risk.
- LWT: topic `status`, payload `offline`, QoS 1, retained (see `status` below).

## Topics

`{id}` = node_id (`esp32-<12 hex>`); `{node}` = a perception node name (e.g. `jetson-yolo`); `{name}` = a service name.

| Topic (after `home/v1/`) | Direction | QoS | Retain | Purpose |
|---|---|---|---|---|
| `nodes/{id}/announce` | node→hub | 1 | yes | Self-description, republished every boot |
| `nodes/{id}/status` | node→hub | 1 | yes | `online` / `offline` (offline via LWT) |
| `nodes/{id}/state` | node→hub | 1 | yes | Values for every endpoint capability |
| `nodes/{id}/cmd` | hub→node | 1 | no | Command |
| `nodes/{id}/ack` | node→hub | 1 | no | Command result |
| `nodes/{id}/ota` | reserved | | | **Not implemented** (D8). Nobody publishes or subscribes. |
| `perception/{node}/events` | node→agent | 1 | no | Detection/change events |
| `services/{name}/health` | service→hub | 0 | yes | CPU, MemAvailable, temp, queue depth |

No other topics exist. There are no `devices/` topics.

## Payloads

JSON Schemas: `announce.schema.json`, `state.schema.json`, `cmd.schema.json`, `ack.schema.json`, `perception-event.schema.json` (samples in `samples/`, checked by `tools\contract-test.ps1`). The prose below is explanatory; the schemas are normative.

**announce**: validated against `announce.schema.json` (schema 2). See `samples/valid/`.

**status**: the bare strings `online` or `offline` (not JSON). LWT: topic `status`, payload `offline`, QoS 1, retained. After connecting the node publishes `online` (retained), see "Connect lifecycle".

## Connect lifecycle

On **every** successful connect, boot and reconnect alike, the node performs these steps in this order:

1. publish `status` = `online` (retained, QoS 1);
2. publish the retained `announce`;
3. subscribe to `nodes/<node_id>/cmd` (QoS 1);
4. publish `state` (every endpoint capability, current values).

**No offline queue.** Nodes do not queue QoS 1 publishes (state, ack) while disconnected; the MQTT client's offline queue is disabled and anything not yet sent is dropped. On reconnect the node publishes fresh `state` as step 4 above. An `ack` lost in an outage is covered by the hub's ack timeout (3 s): the hub reports `ack_timeout` and verifies against the fresh state.

Subscribing to `cmd` only after the announce means the hub never sends a command to a node it has not yet seen describe itself. The hub treats an announce identical to the one it already holds as a no-op; a changed announce is handled by the existing rules (validation, registry diff, re-approval where required).

**state**
```json
{"ts": 1760000000, "endpoints": {"ch1": {"power": true, "speed": 3}, "t1": {"temperature": 27.4}}}
```
`ts` = Unix seconds (UTC, from NTP). Published on change, on heartbeat, and after every command. Contains every capability of every endpoint.

**cmd** (desired state, never toggles; retries are safe)
```json
{"cmd_id": "3f0c...uuid", "endpoint": "ch1", "set": {"power": true, "speed": 3}, "by": "user:vedant"}
{"cmd_id": "...", "cmd": "identify", "duration_s": 10}
{"cmd_id": "...", "cmd": "unprovision"}
```
`by` is `user:<name>`, `agent`, `scene:<id>` or `rule:<id>`. Nodes validate against their own ranges, apply, publish state, ack. **Dedup:** a node keeps the last **16** `cmd_id`s with their cached acks (count-based ring, no TTL, not persisted across reboot). A repeated `cmd_id` is not applied again; the node replays the cached ack. The hub never reuses a `cmd_id` (UUIDv4, one per command). A cmd is never retained or queued by the hub for offline nodes.

**ack**
```json
{"cmd_id": "3f0c...uuid", "ok": true, "endpoint": "ch1", "values": {"power": true, "speed": 3}, "error": null}
```
On failure `ok:false` and `error` is a short code string (`unknown_endpoint`, `unknown_capability`, `out_of_range`, `bad_type`, `busy`, `unsupported`, `bad_request`; free text is not allowed). The hub waits 3 s for an ack. **The ack means "accepted, motion started"**: `values` echo the accepted target, not the position reached. `state` reports actual progress, and for capabilities with `settle_s` the hub verifies convergence within `settle_s` (`mcp-tools.md`). The 3 s timeout is unchanged.

**perception event**
```json
{"ts": 1760000000, "node": "jetson-yolo", "source_id": "cam0", "kind": "detection_change",
 "detections": [{"label": "person", "conf": 0.81, "bbox": [10, 20, 100, 200]}], "summary": "person appeared"}
```

**services health** (retained, interval ~10 s)
```json
{"ts": 1760000000, "service": "vision-node", "cpu_pct": 41.0, "mem_available_mb": 812, "temp_c": 62.5, "queue_depth": 0}
```

## Firmware safety rules (enforced by the firmware, independent of the hub)

- **A physical press wins over failsafe.** A local button/switch press on a capability **cancels that capability's pending failsafe** (the `failsafe_after_s` countdown or an armed failsafe action): the board keeps the value the person chose. The cancellation lasts until the hub next sends a `cmd` that sets that capability; from then on failsafe is armed again for the next link loss. A press made after the failsafe value was already applied simply sets the new value. Local buttons work offline at all times.
- **`max_on_s` is always enforced.** If a capability announces `max_on_s > 0`, the firmware forces it off after that much **continuous** on-time, whatever turned it on: a hub command, a local press, `boot_value`, or `failsafe_value`. Re-asserting "on" while already on does not restart the timer; a new on period starts only after it was off. The forced-off is reported in `state`. A physical press or failsafe never overrides it.
- **Failsafe liveness in v1 is broker-level.** "Hub connection lost" means the node's **MQTT session to the broker is down** (Wi-Fi lost, broker down, TLS failure), counted from the disconnect for `failsafe_after_s`. A dead or hung device-hub process behind a live broker does **not** trigger failsafe in v1. *Future option (not in v1, would need a contract bump and an ACL change): a retained hub heartbeat topic the nodes subscribe to, with failsafe when it goes stale.*

## Firmware time requirement (TLS)

TLS certificate dates are checked against the board's clock, and a board without a valid clock must not guess.

- The firmware embeds its **build timestamp** (`FW_BUILD_TS`, Unix seconds, set at compile time).
- Time obtained from NTP (or any other source) is **valid only if `t >= FW_BUILD_TS`** (and not absurdly far ahead, e.g. `t < FW_BUILD_TS + 50 years`). Anything earlier is treated as "no time".
- Until valid time is obtained the board **does not start the TLS/MQTT connection**. It keeps retrying NTP (LAN `ntp_host` from the provisioning bundle first, then `pool.ntp.org`) with backoff, keeps local buttons and failsafe timers working, and never disables certificate verification or date checks to get online.
- Once the time is valid it is used for `ts` fields; `ts` in `state` is Unix seconds UTC and the schemas reject values before 2001.
- A valid-looking NTP answer that moves the clock backwards past `FW_BUILD_TS` is rejected the same way.
- **Build-time sanity check.** `FW_BUILD_TS` must come from the build host's clock, and the build **aborts** if it is earlier than **2026-10-01T00:00:00Z (1790812800)**, since a build machine with a wrong clock would otherwise ship firmware that accepts 'time' from before the certificates were issued (or never accepts valid time). The floor is a constant in the build script; raise it when the contract is next revised. A build with a clock before that date is a build failure, not a warning.

## ACL rules (enforced through dynamic-security)

- Every node has its own MQTT user (username = `node_id`) and its own role cloned from the template `node-template`, whose ACLs allow publish and subscribe **only** on `home/v1/nodes/<node_id>/#` (dynsec ACL types `publishClientSend`, `publishClientReceive`, `subscribePattern`, `unsubscribePattern`, all on that one subtree).
- A node has **no** access to any other node's subtree, to `perception/`, or to `services/`.
- The hub (`hub-admin`) is privileged: subscribes `home/v1/#`, publishes `nodes/+/cmd`, manages dynsec via `$CONTROL/dynamic-security/v1`. It is the only client allowed on `$CONTROL/#`.
- Perception nodes and services get their own users with roles for `perception/<node>/events` or `services/<name>/health` publish only.
- Forgetting a node deletes its user and role (real revocation), disconnects its client, and clears its retained topics.
- Device strings in payloads are untrusted: length-limited, sanitized, never put raw into LLM prompts.
- **One role per node, not one shared `%u` role (decision, with evidence).** A single shared role using `home/v1/nodes/%u/#` was considered. The dynamic-security documentation says the publish ACL types may use `%c`/`%u` substitution, but in the Mosquitto **v2.0.20** plugin source (`plugins/dynamic-security/acl.c`) `acl_check_publish_c_send` and `acl_check_publish_c_recv` call plain `mosquitto_topic_matches_sub(acl->topic, ...)` with no substitution, so a `%u` publish ACL would match nothing in 2.0.20 (only the subscribe/unsubscribe pattern types go through the pattern checker there). Substitution for publish ACLs (`mosquitto_topic_matches_sub_with_pattern`) exists only on later `master`. We therefore keep a **literal per-node role** `node-<node_id>` created from `deploy/mosquitto/node-role.template.json` (placeholder `{{node_id}}` substituted by the hub's dynsec client, or by `deploy/mosquitto/dynsec-node-user.ps1` for testing). Revisit when the broker image is bumped to a release containing the pattern-aware publish check; `deploy/mosquitto/acl-test.ps1` would prove it.
- Dynsec user/role management by services goes through the dynsec MQTT API (`$CONTROL/dynamic-security/v1`), never through process arguments. `dynsec-node-user.ps1` (docker exec + mosquitto_ctrl, password on the command line) is a dev-PC test helper only.
- **Idempotent user creation.** Any service that creates node users (device-hub for provisioning sessions) must be idempotent: if the client already exists (e.g. a re-provision after a failed attempt or a hardware reset), it sets the new password (`setClientPassword`) and ensures the role exists and is assigned (`createRole` tolerated if present, `addClientRole` tolerated if present) instead of keeping the old password or failing. The credentials returned in the bundle must always be the ones that now work.
- `deploy/mosquitto/dynsec-bootstrap.ps1` creates only the initial config with the hub admin account.

## Contract changelog

- **S0h** (announce stays schema 2; `mqtt.md` changelog only, `gateway.yaml` 2.11.0, `agent-internal.yaml` 1.3.0, `device-hub-internal.yaml` 1.4.0, `mcp-tools.md` v7, `agent-events.md` v8). All additive, nothing removed or renamed:
  - `gateway.yaml`: `GET /admin/action-log` (admin only; filters node/endpoint/user/status/time; cursor pagination; no trace rows); `WsMessage` and `SseEvent` schemas with valid and invalid samples (dashboard types can be generated); `Capability.settle_s`; `409 confirmation_required` on a direct set (and a `needs_confirmation` scene run) carries a server-built `summary`, `action` and `action_hash`; `Route` enum.
  - `agent-internal.yaml`: `allowed_rooms` / `allowed_scenes` in `POST /v1/chat`, enforced by the agent executor (`out_of_scope`), fail closed for member/guest when missing.
  - `mcp-tools.md` / hub: `list_devices` returns `registry_version` and a read-only `scenes` list (still exactly five tools); `settle_s` on capabilities.
  - `agent-events.md`: `final.route` adds `sensor|yolo|vlm`; chat request carries `role` and scope.

- **S0e addendum** (`mqtt.md` v5 (changelog only), `agent-internal.yaml` 1.1.0, `mcp-tools.md` v6, `device-hub-internal.yaml` 1.2.0, `agent-events.md` v7, ARCHITECTURE v6):
  - `agent-internal.yaml`: required `role` (`admin|member|guest`) in `POST /v1/chat` and `POST /v1/confirmations/{request_id}`; set only by the gateway, trusted only from the authenticated gateway call, never inferred from a user id. Tests added.
  - `run_scene` blocked result (`mcp-tools.md`, hub `POST /scenes/{id}/run` 409): also `scene_id`, the canonical `risk: confirm` action list (the exact hashed objects) and the canonical `action_hash`. The `confirmation_request` run_scene action in `agent-events.md` is now `{type, scene_id, actions}` as hashed.
  - ARCHITECTURE 4.10: agent chat body fixed (no `audio_ref` / `image_refs`; inline `images`) and pointed at `agent-internal.yaml`.

- **S0e** (announce stays schema 2; `mqtt.md` v4 (changelog only), `agent-events.md` v7, `gateway.yaml` 2.9.0, new `agent-internal.yaml` 1.0.0, ARCHITECTURE v5):
  - `gateway.yaml`: `SceneAction.target` is exactly one of fixed `{endpoint_id}` or selector `{room?, type?, tag?}` (`minProperties: 1`), both `additionalProperties: false`; samples in `samples/scene-action/` (valid: fixed, selector; invalid: mixed, empty, unknown key) validated by `contract_test.py`.
  - New `openapi/agent-internal.yaml`: the agent's local API on 127.0.0.1:8200 (`POST /v1/chat`, `POST /v1/confirmations/{request_id}`, `GET /v1/events`, `GET /v1/memory`, `DELETE /v1/memory/{id}`, `GET /v1/health`). Auth mandatory on every operation: gateway secret `agent-api.secret` (agent holds a verifier), distinct from the agent's hub secret; no-auth only with `--mock --insecure-no-auth`. The agent recomputes the canonical action hash on approval and refuses on mismatch; it never logs or persists the token. `contract_test.py` checks valid OpenAPI, auth on every operation and the decision body rules.
  - `agent-events.md`: confirmation hand-off sequence; retry is created by the gateway and reaches the agent only at approval with the full action; declined and expired are delivered to the agent; chat request carries inline `images` instead of refs.
  - `gateway.yaml` media: voice becomes a transcript before chat; images are inline `images:[{ref, media_type, data_b64}]` to the agent; limits aligned with the gateway (text 4000, 4 images/turn, image 5 MiB jpeg/png/webp, audio 10 MiB wav/webm/m4a/aac, 120 s single-use TTL, 8 held per user, 32 MiB total); `/transcribe` multipart field is `audio`.
  - `gateway.yaml` admin: `GET/PUT /admin/scenes/{scene_id}/assignments`, `GET/POST /admin/wifi-profiles`, `DELETE /admin/wifi-profiles/{name}` (password write-only, never returned).
  - `gateway.yaml` statuses 429 (+`Retry-After`), 409 (`request_*`, `action_changed`), 413, 415, 502, 504; WebSocket types `confirmation_request`, `confirmation_declined`, `confirmation_expired`, `action_settled`, `action_settle_timeout` (settle events are not notifications).
  - WebSocket auth: JWT never in the URL; `POST /ws/ticket` (single use, 30 s, bound to the user) presented on connect.
  - ARCHITECTURE 4.7/4.11: guests read state in assigned rooms and run assigned scenes, cannot call `POST /devices/{id}/set`; member scene selectors must name an accessible room, re-checked at run time.

- **S0d** (announce stays schema 2; `mqtt.md` v3, `mcp-tools.md` v5, `agent-events.md` v6, `device-hub-internal.yaml` 1.1.0, ARCHITECTURE v4):
  - `device-hub-internal.yaml`: agent scope = exactly `POST /mcp` + read-only `GET /events` with a SERVER-SIDE allowlist (`registry_changed`, `node_status`, `state`, `action_settled`, `action_settle_timeout`), applied to live delivery and replay; a requested type outside it is 403 `forbidden_event_type`; `scenes_changed`, `pending_node`, provisioning/admin and any credential-bearing event are gateway-only. Payloads of every event type documented (including `call_id?` on the two settle events), plus the wire envelope `{seq, type, ts, data}`. Aligned with the S1 hub (names `node_status`/`state`, see the S0d report for discrepancies).
  - `mcp-tools.md`: optional `_meta.call_id` (max 64 chars), set by the agent executor, echoed by the hub on `action_settled` / `action_settle_timeout`, never trusted for any decision.
  - `agent-events.md`: hub settle events map to the agent SSE events with `call_id`; the agent learns registry changes via hub `/events` with the agent scope.
  - `tools\contract_test.py`: asserts the agent scope is exactly `POST /mcp` + `GET /events`, that `/events` documents an allowlist with no admin or provisioning type and every allowlisted payload, and that the file is valid OpenAPI. CLAUDE.md rule 10 and ARCHITECTURE 4.6 reworded for the new agent scope.
  - `--insecure-dev` -> `--insecure-no-auth`: no occurrence exists on this branch or any local branch (only `--insecure-no-auth`), nothing to change.

- **S0c** (announce stays schema 2; `mqtt.md` v2, `mcp-tools.md` v4, `capabilities.md` v2, `gateway.yaml` 2.8.1, new `device-hub-internal.yaml` 1.0.0, ARCHITECTURE v3):
  - `mcp-tools.md`: automations never touch `risk: confirm` endpoints (removed the admin opt-in sentence); `_meta.user_id` added next to `confirmation_token` (set by the executor, forwarded by the hub, never trusted by it, bound-check done by the gateway consume endpoint).
  - Endpoint `tags` (admin-set, max 8, each 1..24 of `[a-z0-9_-]`, not in the announce): OpenAPI `Tag`/`Tags`, admin approve/patch and `Endpoint`, scene selector `tag`, `list_devices` returns `tags`; ARCHITECTURE 4.5/4.6/4.7; `capabilities.md` note; invalid sample `announce` with `tags`.
  - `mcp-tools.md`: `verified` on `set_state`, `scene_name` on `run_scene`, error codes `not_sensor`, `unknown_room`, `unknown_scene`, `scene_broken` (a broken scene refuses to run; offline or capability-less endpoints are still skipped and reported). `gateway.yaml` `SetResult`/`SceneRunResult` aligned.
  - New `openapi/device-hub-internal.yaml`: device-hub's internal API with MANDATORY caller auth and scopes (`gatewaySecret` = everything, `agentSecret` = `POST /mcp` with the five runtime tools only; only `GET /health` is open; no-auth only with `--insecure-no-auth`, accepted only with `--mock`). `tools\contract_test.py` checks it is valid OpenAPI and that the agent scope has no admin operation. CLAUDE.md rule 10 updated; `setup-pi-brain.sh` and `deploy/README.md` create/describe `gateway.secret` and `agent.secret` with hub-readable verifiers.
  - Firmware safety: a physical press cancels the pending failsafe for that capability until the hub next sends a command; `max_on_s` always enforced; failsafe liveness is broker-level in v1 (retained hub heartbeat noted as a future option); build aborts if `FW_BUILD_TS` is earlier than 2026-10-01.

- **S0b** (announce stays schema 2, no nodes deployed; `mcp-tools.md` v3, `agent-events.md` v5, `gateway.yaml` 2.6.0):
  - Node connection rules: `client_id` == `username` == `node_id`; clean session.
  - Connect lifecycle on every connect: status `online`, retained announce, subscribe `cmd`, state. Identical announce is a hub no-op.
  - Nodes do not queue publishes while disconnected; lost acks are covered by the hub's ack timeout.
  - Dedup: last 16 `cmd_id`s, count-based, not persisted; cached ack replayed; hub never reuses a `cmd_id`.
  - Announce: optional `settle_s` (1..120) on actuators, required for `position`. Samples added/updated.
  - Ack means "accepted, motion started"; `set_state` returns `done`/`moving`; `action_settled` and `action_settle_timeout` events; `run_scene` per-action statuses `done|moving|...`.
  - BLE bundle / credentials: username and client_id are both `node_id`; clean session.
  - Idempotent dynsec node-user creation (also in `CLAUDE.md`).

- **S0f** (announce stays schema 2; `mqtt.md` v6, `agent-internal.yaml` 1.2.0, `gateway.yaml` 2.10.0, `device-hub-internal.yaml` 1.3.0):
  - `agent-internal.yaml`: `PUT /v1/memory/{memory_id}` (body `{user_id, text<=500}`; per-user, auth mandatory; another user's memory is 404). The path parameter of `DELETE` is renamed `{id}` -> `{memory_id}` (wire-compatible).
  - `gateway.yaml`: WebSocket type `scenes_changed` `{scene_id, change: created|updated|deleted}`, delivered only to users who can see the scene (rooms and assignments); close code 4429 (rate limited, client backs off); 413 also for an upload with no known Content-Length.
  - `device-hub-internal.yaml`: `SceneRunNotExecuted` schema for the scene-run 409 (requires `executed:false` and `status`; `needs_confirmation` also requires `scene_id`, `needs_confirmation[]`, `action_hash`). New `samples/scene-run-409/` and a `tools\contract_test.py` check; the memory path name in that script was updated.
