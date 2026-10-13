# Agent SSE events (v1)

`POST /v1/chat` (agent, internal; the gateway proxies it as `/chat`) request:
`{"user_id": "...", "session_id": "...", "text?": "...", "audio_ref?": "...", "image_refs?": ["..."]}`
Response: `text/event-stream`. Each event: `event: <name>` + `data: <one-line JSON>`. Every event carries `seq` (monotonic per request) and `req_id`.

| Event | Data | Meaning |
|---|---|---|
| `status` | `{"text": "checking the camera"}` | Human-readable progress. Display only. |
| `tool_call` | `{"call_id", "tool": "set_state", "args": {...}}` | Agent is calling one of the 5 MCP tools. |
| `tool_result` | `{"call_id", "tool", "result": {...}, "ok": bool}` | Result of that call. |
| `confirmation_request` | `{"request_id", "summary": "Unlock front door?", "action": {"type": "set_state", "endpoint_id", "capability", "value"} \| {"type": "run_scene", "scene", "needs_confirmation": [{"endpoint_id", "capability", "value"}]}, "action_hash", "expires_in_s": 60}` | A `risk: confirm` action was blocked (`confirmation_required` / `needs_confirmation`). Nothing has been executed. The UI shows `summary` and the exact action; on approval it calls the gateway `POST /confirmations` with `request_id` (the gateway mints the token and hands it to the agent). The agent executor then re-issues the same call with the token attached out-of-band. If the user declines or it expires, the agent says so. |
| `confirmation_declined` | `{"request_id", "summary"}` | The user declined (gateway `POST /confirmations/{request_id}/decline`). Nothing was executed. The agent replies with a short acknowledgement and does not retry. |
| `confirmation_expired` | `{"request_id", "summary", "reason": "no_response\|token_expired"}` | No answer within `expires_in_s` (`no_response`), or the user approved but the token expired before device-hub consumed it (`token_expired`). Nothing was executed. |
| `action_settled` | `{"call_id", "endpoint_id", "capability", "value", "observed", "elapsed_ms"}` | A motion started by `set_state` / `run_scene` (`status: "moving"`) reached its target within `settle_s` (tolerance in `mcp-tools.md`). `observed` is the last state value seen. May arrive after `final`. |
| `action_settle_timeout` | `{"call_id", "endpoint_id", "capability", "value", "observed", "settle_s"}` | State did not converge to the target within `settle_s`. `observed` is the last state value (or `null` if none arrived). The agent reports it to the user and does not retry on its own. May arrive after `final`. |
| `token` | `{"text": "..."}` | Streamed reply token(s). |
| `final` | `{"text": "...", "route": "fast|llm|vision", "latency_ms": 1234}` | End of the synchronous reply. The stream closes after `final` unless async work is pending. |
| `async_result` | `{"ref": "<call_id>", "kind": "vision", "text": "...", "latency_ms": 5400}` | Late answer (e.g. VLM). May arrive after `final`, also pushed over the gateway WebSocket. |
| `error` | `{"code": "llm_busy|vision_unavailable|budget_exceeded|bad_request", "message": "..."}` | Terminal error; stream closes. |

The `confirmation_token` itself never appears in any event, `tool_call` or `tool_result` payload, and is not part of any tool schema shown to the model (see `mcp-tools.md`).

## Confirmation policy

- **No automatic re-prompt.** After `confirmation_declined` or `confirmation_expired` the agent does not ask again and does not re-issue the call. A new request exists only when the user acts.
- **Retry owner.** Retry is allowed **only for the user who received the original `confirmation_request`**. There is no admin override: another user, including an admin, gets 403 and must start their own request. Retry is also refused for requests created by non-user triggers (those only notify).
- **Retry.** The UI shows a **Retry** button on a declined/expired request. It calls `POST /confirmations/{request_id}/retry`; the agent re-evaluates the same blocked action against the live registry and emits a **new** `confirmation_request` with a new `request_id` and a fresh TTL. Old request ids and tokens stay dead. Retry never executes anything by itself.
- **Proactive and automation-triggered actions never prompt.** If a rule, a perception escalation or any non-user trigger hits a `risk: confirm` endpoint, nothing is executed and no `confirmation_request` is emitted; the system sends a `notification` over the gateway WebSocket instead, e.g. `{"type": "notification", "level": "action_blocked", "text": "Rule 'night lights' wanted to unlock the front door. Needs your confirmation; open the app to do it yourself."}`. The user can then ask in chat, which uses the normal confirmation flow.
- Every outcome (requested, approved, declined, expired, retried, blocked-by-trigger) is written to the audit log.

Rules: the agent never blocks on vision (it emits `final` first, `async_result` later). `route` values mirror the scheduler table. Event payloads never contain secrets or raw device-supplied strings that have not been sanitized. Adding an event name or field is a contract change (bump the header version below).

Contract version: **5** (v5: `action_settled`, `action_settle_timeout`; v4: Retry restricted to the original user, no admin override; v3: `confirmation_declined`, `confirmation_expired`, retry policy; v2: `confirm_request` renamed `confirmation_request`, token-based confirmation)
