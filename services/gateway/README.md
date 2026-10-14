# gateway (S4)

User-facing API (FastAPI, SQLite WAL) for the edge agent system. Public listener `127.0.0.1:8000` (HTTPS is `tailscale serve`'s job), internal listener `127.0.0.1:8001` (or a unix socket) serving **only** `POST /internal/confirmations/consume`. Python 3.11+; all dependencies have aarch64 wheels (checked with `pip download --platform manylinux2014_aarch64 --python-version 3.11`).

Contracts: `contracts/openapi/gateway.yaml` (2.8.1), `device-hub-internal.yaml`, `mcp-tools.md`, `agent-events.md`. Nothing here changes a contract.

## Run (Windows dev PC, PowerShell 5.1)

```powershell
cd services\gateway
py -3.12 -m venv .venv                         # local venv only
.\.venv\Scripts\python -m pip install -r requirements-dev.txt

.\.venv\Scripts\python -m gateway --mock       # fake hub/agent/vision/yolo in-process; demo logins printed once to stderr
.\.venv\Scripts\python -m pytest -q            # ~150 tests, ~80 s (includes the real-hub integration, see below)
```

Real mode (Pi): `python -m gateway serve --config gateway.json` (keys: `gateway/config.py`; secrets only as file paths).

```
python -m gateway --config gw.json bootstrap-admin --username NAME     # one-time; prompts (or --password-stdin); >= 12 chars; refuses if any user exists
python -m gateway --config gw.json wifi-profile add --name home --ssid SSID   # password prompted, stored Fernet-encrypted
python -m gateway --config gw.json assign-scene GUEST_USERNAME SC_ID          # guests may run assigned scenes only
```

Files (all outside git): `data/gateway.db`, `data/jwt.key`, `data/wifi.key` (created mode 600 on first use; keep the key files off the DB backups), `/etc/edge-agent/secrets/gateway.secret` (the gateway's secret for device-hub, read from the file only) and `/etc/edge-agent/secrets/verifiers/device-hub.sha256` (hex SHA-256 of device-hub's secret; the gateway cannot read the secret itself).

## What it does

* **Auth**: argon2id, JWT access (15 min) + opaque rotating refresh tokens (hashed in DB). A refresh token presented twice revokes the whole family. Role and rooms are read from the DB on every request, so edits/disables apply immediately. Login: 5 failures per user+ip and 20 per ip per 15 min -> 429 + `Retry-After`; unknown users cost the same time as wrong passwords.
* **Roles live here** (the hub applies none): `/admin/*` admin only; member scenes only for rooms they can access (room-less selectors are admin-only); member edits only own scenes; guests run assigned scenes only, cannot call `POST /devices/{id}/set`, can never mint a confirmation token, and any token on a guest scene run is stripped.
* **Hub client** (`upstream.py`): `X-Service-Secret` from file, `X-Actor` = user id. `wifi_profile` is resolved to inline Wi-Fi before the hub call. Bodies and tokens are never logged.
* **Confirmations** (`confirmations.py`): only the gateway mints. Tokens: 256-bit, SHA-256 stored, bound to user + hub-computed action hash, 60 s. Hashes always come from the hub (`/devices/{id}/action`, `/scenes/{id}/confirmation`), never from the client or the agent. Agent flow requests are recorded by watching the proxied `/chat` stream. Decline/retry only by the original user (admin gets 403). Consume is one atomic `UPDATE ... RETURNING` (`internal.py`).
* **Chat/voice/images**: SSE passthrough to the agent `/v1/chat` (sessions namespaced per user); images are memory-only (multipart spooling to disk is disabled), 120 s TTL, single use; voice audio is dropped after transcription, only the transcript is kept under the `audio_ref`.
* **WebSocket `/ws`**: auth by `Authorization` header or a first `{"type":"auth","token":...}` message (never a URL parameter); ends when the access token expires. Hub `/events` is mirrored per role/room (`events.py`): `node_status`, `pending_node` admin only; `scenes_changed` not for guests; endpoint events only for rooms the user can access; agent events honour `user_id`/`room`/`admin_only` hints.
* **Audit** (`audit_log` table, ids and reasons only): login, refresh reuse, user/admin ops, device sets, scene ops, every confirmation outcome and consume attempt.

## Tests (`tests\`)

| File | Covers |
|---|---|
| `test_contract.py` | every `gateway.yaml` route exists (and none extra), auth requirement per operation, response schemas validate (all operations, plus the internal consume op) |
| `test_roles.py` | admin/member/guest/anonymous matrix on every route, room and ownership rules |
| `test_auth.py` | refresh rotation and **reuse -> family revoked**, rate limiting, JWT forgery, bootstrap CLI |
| `test_confirmations.py` | lifecycle (approve/decline/retry/expire/stale), **`consume-scenarios.json` loaded from the file**, concurrency (12 racers, one winner), hash vectors |
| `test_ws.py`, `test_media_chat.py`, `test_logs.py` | WS filtering by room/role, chat/voice/images limits, health partials, no secrets in logs/DB at DEBUG |
| `test_listeners.py` | real sockets: `/internal/*` is 404 on the public port, consume works on the internal one, SSE is streamed not buffered |
| `test_integration_hub.py` | **the real device-hub** from `..\edge-agent-s1` (`tests\hub_harness.py`, run with S1's own venv): list devices, approve a node, `set_state`, `risk: confirm` for set_state and scenes through the full token flow, agent-flow hand-off, WS fan-out of real hub events |

The integration uses the hub's in-memory broker and scripted fake nodes (its `--mock` machinery) with caller auth ON and the production `GatewayClient` calling the real internal listener. **No Docker and no real Mosquitto broker were used**, and nothing ran on ARM64 hardware.

## Provisional choices where the contracts are silent (see the S4 report)

* Hand-off of an approved/declined decision to the agent: `POST {agent}/v1/confirmations/{request_id}` `{decision, user_id, confirmation_token?, action, action_hash}` (loopback body only; token is burned if the hand-off fails).
* Agent push stream for late/proactive events: optional `GET {agent}/v1/events` (SSE; `agent_push`, off by default). `async_result` is also mirrored from the chat stream.
* Agent receives uploaded images inline as `images: [{ref, content_type, data_b64}]` next to `image_refs`.
* `/memory` proxies `GET/PUT/DELETE {agent}/v1/memory[/{id}]` with the caller's `user_id`.
* vision-node `/transcribe` multipart field is named `audio`.
