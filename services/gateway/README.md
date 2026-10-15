# gateway (S4)

User-facing API (FastAPI, SQLite WAL) for the edge agent system. Public listener `127.0.0.1:8000` (HTTPS is `tailscale serve`'s job), internal listener `127.0.0.1:8001` or a unix socket, serving **only** `POST /internal/confirmations/consume`. Python 3.11+, every dependency has an aarch64 wheel.

Contracts: `contracts/openapi/gateway.yaml` (2.11.0), `agent-internal.yaml` (1.3.0), `device-hub-internal.yaml`, `mcp-tools.md`, `agent-events.md` (v7). Nothing here changes a contract.

## Run (Windows dev PC, PowerShell 5.1)

```powershell
cd services\gateway
py -3.12 -m venv .venv                         # local venv only
.\.venv\Scripts\python -m pip install -r requirements-dev.txt

.\.venv\Scripts\python -m gateway --mock       # fake hub/agent/vision/yolo in-process; demo logins printed once to stderr
.\.venv\Scripts\python -m pytest -q            # full suite incl. the real-hub integration (needs ..\edge-agent-s1 with its .venv)

# Python 3.11 on emulated linux/arm64 (bookworm), pip --only-binary=:all:, incl. the unix-socket test and the real-hub run:
.\tools\gateway-arm64-test.ps1                 # from the repo root (tools\), starts one uniquely named container, --rm
```

Real mode (Pi): `python -m gateway serve --config gateway.json` (keys: `gateway/config.py`; secrets only as file paths).

```
python -m gateway --config gw.json bootstrap-admin --username NAME     # one-time; prompts (or --password-stdin); >= 12 chars; refuses if any user exists
```

Wi-Fi profiles and scene assignments are admin routes now (`/admin/wifi-profiles`, `/admin/scenes/{id}/assignments`); the older `wifi-profile` / `assign-scene` CLI commands still work offline against the DB.

Files (all outside git): `data/gateway.db`, `data/jwt.key`, `data/wifi.key` (mode 600 on first use), and under `/etc/edge-agent/secrets/`: `gateway.secret` (gateway -> device-hub), `agent-api.secret` (gateway -> agent local API, a different file), `verifiers/device-hub.sha256` (hex SHA-256 of device-hub's secret; the gateway cannot read that secret).

## What it does

* **Auth**: argon2id, JWT access (15 min) + opaque rotating refresh tokens (hashed). A refresh token presented twice revokes the whole family. Role and rooms are read from the DB on every request. Login: 5 failures per user+ip and 20 per ip per 15 min -> 429 + `Retry-After`. Per-user limits (chat, voice, confirmations, tickets, sockets; `rate_limits` in the config) also answer 429 + `Retry-After`.
* **Roles live here**: `/admin/*` admin only; members create scenes only for rooms they can access (room-less selectors are admin-only) and edit only their own; guests run assigned scenes only, cannot call `POST /devices/{id}/set`, and can never mint a confirmation token.
* **Scene actions** are validated strictly (fixed `{endpoint_id}` xor selector `{room?, type?, tag?}`, no unknown keys), the same rule as `contracts/samples/scene-action/`.
* **Confirmations**: the gateway mints (256-bit, SHA-256 stored, user + hub action hash + 60 s). Hashes come from the hub (`/devices/{id}/action`, `/scenes/{id}/confirmation`), never from the client or agent. Agent flow = `POST {agent}/v1/confirmations/{request_id}` with the agent-api secret and `{decision, user_id, role, action, action_hash, confirmation_token?}`; **approved, declined and expired** are all sent; any answer other than 202 burns the token and fails the approval (502). A sweeper expires unanswered requests (`no_response`) and unconsumed tokens (`token_expired`). Retry creates a new request in the gateway; only the original user, no admin override. Consume is one atomic `UPDATE ... RETURNING`.
* **Chat**: `/v1/chat` gets exactly `{user_id, role, session_id, allowed_rooms, allowed_scenes, text?, images?}` (session ids are per user, <= 64 chars). The scope is computed per request: admin `null`/`null` (explicit), member = own rooms + scenes visible to them, guest = own rooms (never null) + assigned scenes. Any lookup error refuses the chat with 502 (fail closed). Voice is a transcript in `text` (audio is dropped after `/transcribe`); images are inline `{ref, media_type, data_b64}`, memory-only, single use, 120 s TTL; the agent never receives `audio_ref`/`image_refs`.
* **Agent events**: the gateway subscribes to the agent `GET /v1/events` by default (`Last-Event-ID`, `reset`), registers `confirmation_request`s, and fans events out. Hub `GET /events` is mirrored too.
* **WebSocket `/ws`**: authenticate with a single-use 30 s ticket from `POST /ws/ticket`, in `Sec-WebSocket-Protocol: edge-ticket.<ticket>` or a first `{"type":"auth","ticket":...}` message (5 s); an `Authorization: Bearer` header also works for header-capable clients. **A JWT in the URL is never accepted.** The socket ends when its access token expires (close 4401). Message types: `state`, `registry_changed`, `notification {level,text}`, `async_result`, `confirmation_request|declined|expired`, and `action_settled` / `action_settle_timeout` as their own types. Filtered by role and room (admin-only: node status, pending nodes; owner events go to the owner only).
* **Action log** (`GET /admin/action-log`, admin only, cursor pagination, filters node/endpoint/user/status/time): one row per device action (direct set, scene run, agent `set_state`/`run_scene` seen in the proxied chat stream, declined/expired confirmations); `moving` rows are closed in place by settle events (hub or agent, by `call_id` else endpoint+capability+value). Ids, enums and error codes only: no chat text, prompts, tool args beyond the action, tokens or turn traces. Reading it is audit-logged.
* **409s**: a direct set on a `risk: confirm` endpoint and a scene-run needing confirmation both answer with a server-built `summary` (sanitized registry labels) and the hub's `action_hash` (+ `action` for the set), the same values `POST /confirmations` mints for.
* **WebSocket/SSE conformance**: every WS message is built by a sanitizer that emits exactly the `WsMessage` shape (malformed upstream events are dropped); `tests/test_v211.py` validates real emitted WS messages and proxied SSE events against the contract schemas. Capability `settle_s` is passed through only on actuators with a 1..120 value.
* **Audit** (`audit_log`, ids and reasons only) for login, refresh reuse, admin/user/scene/Wi-Fi ops, device sets and every confirmation outcome.

## Tests (`tests\`)

`EDGE_CONTRACTS_DIR` points the contract tests at another `contracts/` checkout (needed while the 2.11.0 contracts live only on branch `s0h-contracts`).

| File | Covers |
|---|---|
| `test_contract.py` | every `gateway.yaml` route exists (none extra), auth requirement per operation, response schemas validate for all operations, strict scene targets and the contract's scene-action samples |
| `test_roles.py` | admin/member/guest/anonymous matrix on every route, room and ownership rules, scene assignment routes |
| `test_auth.py` | refresh rotation and reuse -> family revoked, rate limiting, JWT forgery, bootstrap CLI |
| `test_confirmations.py` | lifecycle incl. expired/declined hand-off, 202-only hand-off, `consume-scenarios.json` loaded from the file, 12-way concurrency, hash vectors |
| `test_ws.py` | ticket auth (single use, TTL, no JWT/ticket in the URL), filtering by room/role, hub and agent event fan-out end to end |
| `test_media_chat.py`, `test_logs.py` | chat/voice/image contract and limits, new statuses, Wi-Fi profile routes, no secret in logs/DB at DEBUG |
| `test_v211.py` | 2.11.0: action log (filters, cursor, no traces, settle in place), WsMessage/SseEvent conformance of real output, `settle_s`, 409 summary/action/hash, chat scope fail-closed |
| `test_listeners.py` | real sockets: `/internal/*` 404 on the public port, SSE streamed not buffered, consume over a unix socket (skipped on Windows, runs in the arm64 container) |
| `test_integration_hub.py` | the REAL device-hub from `..\edge-agent-s1` (`tests\hub_harness.py`): list devices, approve a node, `set_state`, `risk: confirm` for devices and scenes, agent flow with the hub's `run_scene` action and hash, WS with tickets on real sockets |

The integration uses the hub's in-memory broker and scripted fake nodes (its `--mock` machinery) with caller auth ON and the production `GatewayClient` calling the real internal listener. No real Mosquitto broker is involved.

## Known gaps

* `gateway.yaml` has `PUT /memory/{memory_id}` but `agent-internal.yaml` has no matching agent route (only GET and DELETE). The gateway forwards `PUT /v1/memory/{id}`; that call is not covered by the contract.
* vision-node `/transcribe`'s multipart field name (`audio`) is assumed.
