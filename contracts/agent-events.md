# Agent SSE events (v1)

`POST /v1/chat` (agent, internal; the gateway proxies it as `/chat`) request:
`{"user_id": "...", "role": "admin|member|guest", "session_id": "...", "allowed_rooms?": [...] | null, "allowed_scenes?": [...] | null, "text?": "...", "images?": [{"ref", "media_type", "data_b64"}]}` (see `openapi/agent-internal.yaml`). Voice is already a transcript in `text`; images are inline (max 4, 5 MiB each, `image/jpeg|png|webp`); the agent receives no audio and no refs to fetch.
Response: `text/event-stream`. Each event: `event: <name>` + `data: <one-line JSON>`. Every event carries `seq` (monotonic per request) and `req_id`.

| Event | Data | Meaning |
|---|---|---|
| `status` | `{"text": "checking the camera"}` | Human-readable progress. Display only. |
| `tool_call` | `{"call_id", "tool": "set_state", "args": {...}}` | Agent is calling one of the 5 MCP tools. |
| `tool_result` | `{"call_id", "tool", "result": {...}, "ok": bool}` | Result of that call. |
| `confirmation_request` | `{"request_id", "summary": "Unlock front door?", "action": {"type": "set_state", "endpoint_id", "capability", "value"} \| {"type": "run_scene", "scene_id", "actions": [{"endpoint_id", "capability", "value"}]}, "action_hash", "expires_in_s": 60}` | A `risk: confirm` action was blocked (`confirmation_required` / `needs_confirmation`). Nothing has been executed. `action` is exactly the object that is hashed (for `run_scene`: the `scene_id` and the canonical `risk: confirm` actions from the `needs_confirmation` result, `mcp-tools.md`). The UI shows `summary` and the exact action; on approval it calls the gateway `POST /confirmations` with `request_id` (the gateway mints the token and hands it to the agent). The agent executor then re-issues the same call with the token attached out-of-band. If the user declines or it expires, the agent says so. |
| `confirmation_declined` | `{"request_id", "summary"}` | The user declined (gateway `POST /confirmations/{request_id}/decline`). Nothing was executed. The agent replies with a short acknowledgement and does not retry. |
| `confirmation_expired` | `{"request_id", "summary", "reason": "no_response\|token_expired"}` | No answer within `expires_in_s` (`no_response`), or the user approved but the token expired before device-hub consumed it (`token_expired`). Nothing was executed. |
| `action_settled` | `{"call_id", "endpoint_id", "capability", "value", "observed", "elapsed_ms"}` | A motion started by `set_state` / `run_scene` (`status: "moving"`) reached its target within `settle_s` (tolerance in `mcp-tools.md`). `observed` is the last state value seen. May arrive after `final`. |
| `action_settle_timeout` | `{"call_id", "endpoint_id", "capability", "value", "observed", "settle_s"}` | State did not converge to the target within `settle_s`. `observed` is the last state value (or `null` if none arrived). The agent reports it to the user and does not retry on its own. May arrive after `final`. |
| `token` | `{"text": "..."}` | Streamed reply token(s). |
| `final` | `{"text": "...", "route": "fast|llm|vision|sensor|yolo|vlm", "latency_ms": 1234}` | End of the synchronous reply. The stream closes after `final` unless async work is pending. |
| `async_result` | `{"ref": "<call_id>", "kind": "vision", "text": "...", "latency_ms": 5400}` | Late answer (e.g. VLM). May arrive after `final`, also pushed over the gateway WebSocket. |
| `error` | `{"code": "llm_busy|vision_unavailable|budget_exceeded|bad_request", "message": "..."}` | Terminal error; stream closes. |

The `confirmation_token` itself never appears in any event, `tool_call` or `tool_result` payload, and is not part of any tool schema shown to the model (see `mcp-tools.md`).

## Confirmation policy

- **No automatic re-prompt.** After `confirmation_declined` or `confirmation_expired` the agent does not ask again and does not re-issue the call. A new request exists only when the user acts.
- **Retry owner.** Retry is allowed **only for the user who received the original `confirmation_request`**. There is no admin override: another user, including an admin, gets 403 and must start their own request. Retry is also refused for requests created by non-user triggers (those only notify).
- **Retry.** The UI shows a **Retry** button on a declined/expired request. It calls `POST /confirmations/{request_id}/retry`; the gateway creates a **new** request for the stored action (live registry re-validation) with a new `request_id` and a fresh TTL; the agent learns of it only at approval (see the hand-off below). Old request ids and tokens stay dead. Retry never executes anything by itself.
- **Proactive and automation-triggered actions never prompt.** If a rule, a perception escalation or any non-user trigger hits a `risk: confirm` endpoint, nothing is executed and no `confirmation_request` is emitted; the system sends an `automation_notification` over the gateway WebSocket instead (`contracts/automations.md`; rules never target `risk: confirm` endpoints with `set_state`, and the run is logged as `blocked_confirm`), e.g. `{"type": "automation_notification", "level": "action_blocked", "source": "automation:ru_0a1b2c3d", "text": "Rule 'night lights' wanted to unlock the front door. Needs your confirmation; open the app to do it yourself."}`. The user can then ask in chat, which uses the normal confirmation flow.
- Every outcome (requested, approved, declined, expired, retried, blocked-by-trigger) is written to the audit log.

## Confirmation hand-off (gateway <-> agent)

Transport: the agent's local API (`openapi/agent-internal.yaml`): `POST /v1/confirmations/{request_id}` and the `GET /v1/events` stream. The user never talks to the agent directly.

1. The agent blocks a `risk: confirm` action and emits `confirmation_request` (`request_id`, `summary`, full `action`, `action_hash`, `expires_in_s`) on the chat stream. It records the request as pending for that `user_id`. The gateway records the request (id, user, action, hash, expiry) from the proxied stream; the action hash it uses is the one computed by device-hub, not the agent's claim.
2. The user approves (`POST /confirmations {request_id}`). The gateway checks that the caller is the user the request was issued to and that it is neither expired nor handled, mints the single-use token bound to user + hash, and calls the agent `POST /v1/confirmations/{request_id}` with `{decision: "approved", user_id, action, action_hash, confirmation_token}`. The body always carries the **full action**, so the agent needs no memory of the request to act on it.
3. The agent recomputes the canonical hash from `action` (`mcp-tools.md`). If it differs from `action_hash` it refuses (409 `action_hash_mismatch`), executes nothing and the token is left to expire. Otherwise it re-issues the call with the token attached out-of-band (never in a prompt, tool schema, event or log), drops the token from memory, and answers 202.
4. Device-hub consumes the token at the gateway; the outcome arrives as `tool_result` / `action_settled` / `action_settle_timeout` / `async_result`. If the original chat stream is closed, they are delivered on `GET /v1/events`, tagged with `user_id`, and the gateway pushes them to that user's WebSocket.

**Declined and expired are delivered too.** `POST /confirmations/{request_id}/decline` makes the gateway send `{decision: "declined", user_id, action, action_hash}` (no token); when the TTL passes with no answer, or the token expired before consumption, the gateway sends `{decision: "expired", ...}`. The agent clears its pending state for `request_id`, emits `confirmation_declined` / `confirmation_expired`, and does not retry.

**Retry.** `POST /confirmations/{request_id}/retry` is handled by the **gateway**: it creates the new request (new `request_id`, fresh TTL) for the stored action, re-validated against the live registry, and shows it to the user. The agent does not know about the new request until the user approves it; at approval the gateway sends the normal approved hand-off for the new `request_id`, which carries the full `action` and `action_hash`, and the agent executes it exactly as in step 3 (it does not need the old request). A retry that was never approved leaves no trace in the agent.

## Where the agent gets settle and registry events

The agent subscribes to device-hub `GET /events` with its **agent secret** (read-only, server-side allowlist: `registry_changed`, `node_status`, `state`, `action_settled`, `action_settle_timeout`; see `openapi/device-hub-internal.yaml`). It never sees provisioning, admin or scene-CRUD events.

**Settle events.** For every `set_state` / `run_scene` action that returned `status: "moving"` the executor sent `_meta.call_id` (see `mcp-tools.md`). The hub echoes it, and the agent forwards the event on the SSE stream of the request that made the call:

| device-hub event (`data.data`) | agent SSE event | Mapping |
|---|---|---|
| `action_settled` `{endpoint_id, capability, value, observed, elapsed_ms, call_id}` | `action_settled` `{call_id, endpoint_id, capability, value, observed, elapsed_ms}` | Same fields; `call_id` selects the request/stream by the executor's own `call_id` table. |
| `action_settle_timeout` `{endpoint_id, capability, value, observed, settle_s, call_id}` | `action_settle_timeout` `{call_id, endpoint_id, capability, value, observed, settle_s}` | Same fields. |

If the hub event has no `call_id` (call without `_meta.call_id`, or the agent restarted and lost its table) the agent cannot attribute it to a request: it does not emit an SSE event for it and instead sends a gateway WebSocket `notification` when the outcome is a timeout. The agent never retries on its own after `action_settle_timeout`. Values in these events come from the registry/state (sanitized), never from free device text.

**Registry changes.** The agent learns of them only from `registry_changed` `{version, reason, endpoint_ids}` on that stream. On receipt it reloads `list_devices` and rebuilds its prompt device list and constrained-decoding schema (so renamed, added, forgotten, disabled or re-risked endpoints take effect without a restart). It reconnects with `Last-Event-ID`; if the id is stale or the `hello.version` differs from its cached version it reloads anyway. `node_status` updates `online` in the cached list; `state` is opt-in and the agent normally uses `get_state` instead. No other source (admin routes, MQTT) is available to the agent.

Rules: the agent never blocks on vision (it emits `final` first, `async_result` later). `route` values mirror the scheduler table: `fast`, `llm`, `sensor`, `yolo`, `vlm`; `vision` is kept as a legacy umbrella and clients treat an unknown value as opaque. Every client-visible message and event is also specified as a schema in `openapi/gateway.yaml` (`SseEvent`, `WsMessage`). Event payloads never contain secrets or raw device-supplied strings that have not been sanitized. Adding an event name or field is a contract change (bump the header version below).

Contract version: **9** (v9: automation-triggered blocks are `automation_notification` messages, see `automations.md`; no event or field added to the agent stream; v8: `final.route` gains `sensor|yolo|vlm` (additive; `vision` stays), chat request carries `role`, `allowed_rooms`, `allowed_scenes`; v7: `run_scene` confirmation action is `{scene_id, actions}` as hashed; confirmation hand-off sequence via `agent-internal.yaml`, retry owned by the gateway, declined/expired delivered to the agent, inline `images` in the chat request; v6: settle events carry the `call_id` of the originating call and are mapped from device-hub `GET /events`; registry-change source documented; v5: `action_settled`, `action_settle_timeout`; v4: Retry restricted to the original user, no admin override; v3: `confirmation_declined`, `confirmation_expired`, retry policy; v2: `confirm_request` renamed `confirmation_request`, token-based confirmation)
