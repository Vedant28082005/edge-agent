# MQTT contract (v1)

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

**status**: the bare strings `online` or `offline` (not JSON). LWT: topic `status`, payload `offline`, QoS 1, retained. After connecting the node publishes `online` (retained).

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
`by` is `user:<name>`, `agent`, `scene:<id>` or `rule:<id>`. Nodes dedup on `cmd_id`, validate against their own ranges, apply, publish state, ack. A cmd is never retained or queued by the hub for offline nodes.

**ack**
```json
{"cmd_id": "3f0c...uuid", "ok": true, "endpoint": "ch1", "values": {"power": true, "speed": 3}, "error": null}
```
On failure `ok:false` and `error` is a short code string (`unknown_endpoint`, `unknown_capability`, `out_of_range`, `bad_type`, `busy`, `unsupported`, `bad_request`; free text is not allowed). The hub waits 3 s for an ack.

**perception event**
```json
{"ts": 1760000000, "node": "jetson-yolo", "source_id": "cam0", "kind": "detection_change",
 "detections": [{"label": "person", "conf": 0.81, "bbox": [10, 20, 100, 200]}], "summary": "person appeared"}
```

**services health** (retained, interval ~10 s)
```json
{"ts": 1760000000, "service": "vision-node", "cpu_pct": 41.0, "mem_available_mb": 812, "temp_c": 62.5, "queue_depth": 0}
```

## Firmware time requirement (TLS)

TLS certificate dates are checked against the board's clock, and a board without a valid clock must not guess.

- The firmware embeds its **build timestamp** (`FW_BUILD_TS`, Unix seconds, set at compile time).
- Time obtained from NTP (or any other source) is **valid only if `t >= FW_BUILD_TS`** (and not absurdly far ahead, e.g. `t < FW_BUILD_TS + 50 years`). Anything earlier is treated as "no time".
- Until valid time is obtained the board **does not start the TLS/MQTT connection**. It keeps retrying NTP (LAN `ntp_host` from the provisioning bundle first, then `pool.ntp.org`) with backoff, keeps local buttons and failsafe timers working, and never disables certificate verification or date checks to get online.
- Once the time is valid it is used for `ts` fields; `ts` in `state` is Unix seconds UTC and the schemas reject values before 2001.
- A valid-looking NTP answer that moves the clock backwards past `FW_BUILD_TS` is rejected the same way.

## ACL rules (enforced through dynamic-security)

- Every node has its own MQTT user (username = `node_id`) and its own role cloned from the template `node-template`, whose ACLs allow publish and subscribe **only** on `home/v1/nodes/<node_id>/#` (dynsec ACL types `publishClientSend`, `publishClientReceive`, `subscribePattern`, `unsubscribePattern`, all on that one subtree).
- A node has **no** access to any other node's subtree, to `perception/`, or to `services/`.
- The hub (`hub-admin`) is privileged: subscribes `home/v1/#`, publishes `nodes/+/cmd`, manages dynsec via `$CONTROL/dynamic-security/v1`. It is the only client allowed on `$CONTROL/#`.
- Perception nodes and services get their own users with roles for `perception/<node>/events` or `services/<name>/health` publish only.
- Forgetting a node deletes its user and role (real revocation), disconnects its client, and clears its retained topics.
- Device strings in payloads are untrusted: length-limited, sanitized, never put raw into LLM prompts.
- **One role per node, not one shared `%u` role (decision, with evidence).** A single shared role using `home/v1/nodes/%u/#` was considered. The dynamic-security documentation says the publish ACL types may use `%c`/`%u` substitution, but in the Mosquitto **v2.0.20** plugin source (`plugins/dynamic-security/acl.c`) `acl_check_publish_c_send` and `acl_check_publish_c_recv` call plain `mosquitto_topic_matches_sub(acl->topic, ...)` with no substitution, so a `%u` publish ACL would match nothing in 2.0.20 (only the subscribe/unsubscribe pattern types go through the pattern checker there). Substitution for publish ACLs (`mosquitto_topic_matches_sub_with_pattern`) exists only on later `master`. We therefore keep a **literal per-node role** `node-<node_id>` created from `deploy/mosquitto/node-role.template.json` (placeholder `{{node_id}}` substituted by the hub's dynsec client, or by `deploy/mosquitto/dynsec-node-user.ps1` for testing). Revisit when the broker image is bumped to a release containing the pattern-aware publish check; `deploy/mosquitto/acl-test.ps1` would prove it.
- Dynsec user/role management by services goes through the dynsec MQTT API (`$CONTROL/dynamic-security/v1`), never through process arguments. `dynsec-node-user.ps1` (docker exec + mosquitto_ctrl, password on the command line) is a dev-PC test helper only. `deploy/mosquitto/dynsec-bootstrap.ps1` creates only the initial config with the hub admin account.
