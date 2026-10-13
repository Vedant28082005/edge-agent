# device-hub (S1)

Registry, MQTT bridge, Mosquitto dynamic-security client, provisioning sessions, executor (`set_state`), scenes, the five-tool MCP server, and the loopback HTTP API the gateway calls. Python 3.11+, SQLite (WAL), `127.0.0.1:8100`.

Read first: `CLAUDE.md`, `contracts/` (nothing here changes a contract). The proposal for the hub's own HTTP interface is [INTERNAL_API.md](INTERNAL_API.md).

## Run on the Windows dev PC (PowerShell 5.1)

```powershell
cd services\device-hub
py -3.12 -m venv .venv                       # local venv only, never global installs
.\.venv\Scripts\python -m pip install -r requirements-dev.txt

# Mock mode: in-memory broker + fake gateway + two demo nodes, no network except 127.0.0.1:8100
.\.venv\Scripts\python -m device_hub --mock
curl.exe http://127.0.0.1:8100/devices
.\.venv\Scripts\python -m device_hub --mock --mcp-stdio     # MCP over stdio instead (for an MCP inspector)

# Unit tests (no broker, no Docker)
.\.venv\Scripts\python -m pytest -m "not integration"

# Integration test against the dev broker (see deploy/README.md; generate dev certs + dynsec first)
cd ..\..\deploy; docker compose -f compose.yml -f compose.dev.yml up -d; cd ..\services\device-hub
.\.venv\Scripts\python -m pytest -m integration -rs     # skipped (not passed) if the broker is unreachable
```

Integration settings (defaults match `compose.dev.yml`): `EDGE_IT_HOST=localhost`, `EDGE_IT_PORT=8883`, `EDGE_IT_CA=deploy\mosquitto\certs\ca.crt`, `EDGE_IT_ENV=deploy\mosquitto\.env`.

## Real mode

```powershell
.\.venv\Scripts\python -m device_hub --config hub.json
```

`config.example.json` lists every key. Secrets are **only referenced by file path**, never put in the config, env or argv:

| Key | Meaning |
|---|---|
| `mqtt.ca_file`, `mqtt.host/port`, `mqtt.username`, `mqtt.password_file` | hub's own TLS MQTT account (`hub-admin`, privileged). |
| `provision.broker_host/broker_port/ntp_host` | what goes into the BLE bundle: the broker's **LAN** address the ESP32 can reach. `provision.ca_file` defaults to `mqtt.ca_file`. |
| `gateway.url`, `gateway.secret_file` | internal gateway listener (`http://127.0.0.1:8001` or `unix:///run/edge-agent/gateway-internal.sock`) and `/etc/edge-agent/secrets/device-hub.secret` (mode 600, device-hub user). If the file is unreadable every `risk: confirm` action stays blocked. |
| `auth.verifier_dir` | optional caller authentication, see INTERNAL_API.md. |
| `ack_timeout_s` (3), `stale_factor` (3), `provision.ttl_s` (600) | contract values. |

## Layout

| Module | Role |
|---|---|
| `contracts.py` | loads `announce/state/cmd/ack` schemas from `contracts/`, hub-enforced announce rules, vocabulary, value coercion |
| `registry.py`, `db.py` | SQLite registry (nodes, endpoints, capabilities, rooms, audit_log; scenes in `scenes.py`, sessions in `provisioning.py`), live online/state cache |
| `bridge.py`, `transport.py` | MQTT in/out over TLS (paho), ack correlation, never queues a command |
| `dynsec.py` | user + per-node role create/delete over `$CONTROL/dynamic-security/v1` (no process arguments) |
| `provisioning.py` | sessions, bundle, announce matching, 10-min cleanup |
| `executor.py` | `set_state` (validate, confirm, send, ack, read back), `get_state`, `read_sensor`, `list_devices` |
| `scenes.py` | fixed + selector targets, all-or-nothing confirmation, re-validation, usage |
| `admin.py` | approve, patch, rebind, identify, unprovision, forget |
| `mcp_server.py` | exactly five tools; imports only the executor and scene service |
| `api.py`, `events.py` | internal HTTP API, SSE registry-change stream |
| `mock.py` | fake broker (dynsec + ACL emulation), scripted fake node, fake gateway |

## Behaviour worth knowing

- **Names, rooms, aliases are admin-only.** An announce carrying any such field is rejected by the schema. Every device string (hw, fw, state/ack values) is schema-restricted, then sanitised and length-limited; admin labels get the same sanitising.
- **Validation order for `set_state`**: id, enabled, capability, actuator, type/range, online, then the confirmation token (consumed last, so an invalid request never burns an approval), then send, ack (3 s), read-back compare.
- **`risk: confirm`**: the token comes only from MCP `_meta` / the API body, is validated by `POST /internal/confirmations/consume` using the canonical RFC 8785 hash computed **by the hub from its own view**. Any denial, network error or malformed reply means "no token" (fail closed).
- **Scenes** run per node in parallel, sequentially within a node. Offline → `skipped_offline`, missing/non-actuator capability → `skipped_no_capability`, never queued. A scene with a fixed target that no longer exists or lost the capability is flagged `broken` on the next registry change and refuses to run (`scene_broken`) until edited.
- **Forget** deletes the MQTT user and role through dynsec (the broker disconnects the client), clears retained `announce/status/state`, soft-deletes rows (ids stay reserved), and flags scenes. Messages from a forgotten node are ignored even if it somehow still can publish.
- **Broker link down** → every endpoint reports offline and no command is sent (publishing while disconnected raises instead of queueing).
- **No secrets in logs**: MQTT passwords, Wi-Fi passwords, the service secret and tokens are registered with a log redactor in addition to never being logged on purpose; paho's own debug logger is never enabled. `tests/test_logs.py` runs the provisioning/confirmation/failure paths at DEBUG and greps logs, stdout/stderr and the DB.

## Not covered here

Gateway, agent, firmware, BLE, dashboards. No Dockerfile, systemd unit or compose entry for the hub (deploy is S0's area), no automations table (the usage check reports `automations: 0`), no `users`/`user_rooms`/`sources` tables (those belong to gateway and perception). Tested on Windows/CPython 3.12 and a Linux container; ARM64 results are in the S1 report.
