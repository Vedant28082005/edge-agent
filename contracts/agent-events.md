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
| `token` | `{"text": "..."}` | Streamed reply token(s). |
| `final` | `{"text": "...", "route": "fast|llm|vision", "latency_ms": 1234}` | End of the synchronous reply. The stream closes after `final` unless async work is pending. |
| `async_result` | `{"ref": "<call_id>", "kind": "vision", "text": "...", "latency_ms": 5400}` | Late answer (e.g. VLM). May arrive after `final`, also pushed over the gateway WebSocket. |
| `error` | `{"code": "llm_busy|vision_unavailable|budget_exceeded|bad_request", "message": "..."}` | Terminal error; stream closes. |

The `confirmation_token` itself never appears in any event, `tool_call` or `tool_result` payload, and is not part of any tool schema shown to the model (see `mcp-tools.md`).

Rules: the agent never blocks on vision (it emits `final` first, `async_result` later). `route` values mirror the scheduler table. Event payloads never contain secrets or raw device-supplied strings that have not been sanitized. Adding an event name or field is a contract change (bump the header version below).

Contract version: **2** (v2: `confirm_request` renamed `confirmation_request`, token-based confirmation)
