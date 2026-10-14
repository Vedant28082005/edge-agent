# S4 gateway: report (S0e + S0f)

## Update after contracts S0f (commit `27886ba`)
- Adapted to gateway 2.10.0, agent-internal 1.2.0, device-hub 1.3.0: `PUT /memory/{memory_id}` forwards `PUT /v1/memory/{memory_id}` exactly per agent-internal; `scenes_changed {scene_id, change}` is sent only to users who can see the scene (judged at delivery; a deleted scene by its last known content); close code 4429 for connect and inbound-message floods; 413 for an upload with unknown Content-Length; the hub's 409 body validates against `SceneRunNotExecuted`. `contracts/` untouched.
- **Windows, Python 3.12 [measured]:** 191 passed, 1 skipped (the unix-socket test needs Linux), including the real-hub integration.
- **Python 3.11.17, emulated linux/arm64 bookworm container [measured]** (`tools\gateway-arm64-test.ps1`, `pip --only-binary=:all:`, sqlite 3.40.1, run on `27886ba`): **192 passed, 0 skipped, 7m57s**. That includes the unix-socket consume test and the real-hub integration (S1 hub in its own arm64 venv). Emulated, not real hardware: timings are not representative and nothing ran on a Pi.
- The memory-PUT gap and the `scenes_changed` / 4429 / 411 decisions listed further down are resolved by S0f.

---


**Built (all 10 items):** merged `main`, then adapted `services/gateway` to gateway.yaml 2.9.0 and agent-internal.yaml 1.1.0. `contracts/` untouched.
1. Scene targets are validated strictly (fixed xor selector, no unknown keys); the `anyOf` workaround is gone and the contract's `samples/scene-action/{valid,invalid}` are loaded by a test.
2. The agent hand-off follows agent-internal.yaml. It authenticates with `agent-api.secret` (read from a file). It sends `{decision, user_id, role, action, action_hash, confirmation_token?}` for approved, declined and expired. Only a 202 counts; any other answer burns the token and returns 502. A sweeper sends `expired` (`no_response` / `token_expired`) and pushes `confirmation_expired` over the WebSocket.
3. `/v1/chat` receives exactly `{user_id, role, session_id, text?, images?}` (session ids are per user and 64 characters or fewer).
4. Inline images use `media_type`. The agent never receives `image_refs` or `audio_ref`; voice is a transcript in `text`.
5. The gateway subscribes to the agent `GET /v1/events` by default, with `Last-Event-ID` and `reset` handling. It registers `confirmation_request` events and fans events out to `/ws`, which sends payloads limited to the contract fields. Settle events are their own message types.
6. Admin routes added: `GET/PUT /admin/scenes/{id}/assignments` and `GET/POST /admin/wifi-profiles`, plus `DELETE /admin/wifi-profiles/{name}`. Passwords are write-only and are never returned, logged or audited.
7. WebSocket auth now uses `POST /ws/ticket`: single use, 30 s, bound to the user. The ticket goes in `Sec-WebSocket-Protocol: edge-ticket.<t>` or a first `{"type":"auth","ticket":...}` message (5 s). A JWT in the URL is never accepted. Before this change, auth was an `Authorization: Bearer <JWT>` header, or a first message carrying the JWT as `token`, and never the URL. A bearer header is still accepted for clients that can send headers, as the contract allows.
8. New statuses and limits are in: 429 with `Retry-After` (login, chat, voice, confirmations, tickets, sockets, upload quota), 413, 415, and 502/504 with `upstream_error` / `upstream_timeout` / `vision_unavailable`. Confirmation conflicts use `request_expired`, `request_already_handled`, `request_not_retryable` and `action_changed`. Voice type is sniffed, not read from Content-Type. Media limits are 10 MiB audio, 5 MiB per image, 8 images per user, 32 MiB total.
9. Risky scenes use the hub's `/scenes/{id}/confirmation` hash and action. A real-hub test checks it equals the `action_hash` in the hub's 409 body.

**Tested [measured], Windows, Python 3.12:** full suite 182 passed, 1 skipped (the unix-socket test, which needs Linux). The arm64 run below covers that test.
- Real-hub integration: 13 passed against the S1 device-hub from `..\edge-agent-s1`.
  - Single devices: list devices, approve a node, `set_state`, `risk: confirm`.
  - Scenes: `risk: confirm` scenes, the agent-flow hand-off with the hub's `run_scene` action and hash, and a stale-request case.
  - Plumbing: caller auth on, WebSocket tickets on real sockets, and a log scan for secrets.
- The hub side uses an in-memory broker and scripted fake nodes. No real Mosquitto broker was involved.

**Tested [measured], Python 3.11.17 in an emulated linux/arm64 bookworm container** (`tools\gateway-arm64-test.ps1`, `pip --only-binary=:all:`, sqlite 3.40.1): 183 passed, 0 skipped, 7m44s.
- That run includes the unix-socket consume test (it skips on Windows, so this is its first real run) and the real-hub integration (S1 hub in its own arm64 venv, 13 tests).
- Every dependency installed from wheels, for the gateway and for the S1 hub.
- Emulated arm64, not real hardware: timings are not representative and nothing ran on a Pi.

**Commits:** `2fba103` is the adaptation, then the `arm64-test.sh` fix, then this report. Working tree clean. Nothing is pushed or merged.

**Decisions for you:**
1. **Memory update gap.** `gateway.yaml` has `PUT /memory/{memory_id}` but `agent-internal.yaml` has no `PUT /v1/memory/{id}` (only GET and DELETE). The gateway forwards the PUT anyway, which is not covered by the contract. The contract needs a PUT or the gateway route should go.
2. **Hub settle events.** Settle events from the hub that carry a `call_id` are skipped, because the agent stream delivers them to the requesting user. Those without one go to everyone who can see the endpoint's room. Hub `scenes_changed` is dropped because it isn't a WebSocket message in the contract.
3. **Close codes and missing length.** A socket that is rate-limited closes with 4429, which the contract doesn't name. An upload without a known Content-Length gets 413.
4. **Vision field name.** I assumed vision-node `/transcribe` takes a multipart field named `audio`.
