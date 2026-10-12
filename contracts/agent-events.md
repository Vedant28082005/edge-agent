# Agent SSE events (v1)

`POST /v1/chat` (agent, internal; the gateway proxies it as `/chat`) request:
`{"user_id": "...", "session_id": "...", "text?": "...", "audio_ref?": "...", "image_refs?": ["..."]}`
Response: `text/event-stream`. Each event: `event: <name>` + `data: <one-line JSON>`. Every event carries `seq` (monotonic per request) and `req_id`.

| Event | Data | Meaning |
|---|---|---|
| `status` | `{"text": "checking the camera"}` | Human-readable progress. Display only. |
| `tool_call` | `{"call_id", "tool": "set_state", "args": {...}}` | Agent is calling one of the 5 MCP tools. |
| `tool_result` | `{"call_id", "tool", "result": {...}, "ok": bool}` | Result of that call. |
| `confirm_request` | `{"call_id", "summary": "Unlock front door?", "action": {...}}` | Needs user confirmation (`risk: confirm`). The UI answers via the gateway; the agent then continues with `confirmed:true` or aborts. |
| `token` | `{"text": "..."}` | Streamed reply token(s). |
| `final` | `{"text": "...", "route": "fast|llm|vision", "latency_ms": 1234}` | End of the synchronous reply. The stream closes after `final` unless async work is pending. |
| `async_result` | `{"ref": "<call_id>", "kind": "vision", "text": "...", "latency_ms": 5400}` | Late answer (e.g. VLM). May arrive after `final`, also pushed over the gateway WebSocket. |
| `error` | `{"code": "llm_busy|vision_unavailable|budget_exceeded|bad_request", "message": "..."}` | Terminal error; stream closes. |

Rules: the agent never blocks on vision (it emits `final` first, `async_result` later). `route` values mirror the scheduler table. Event payloads never contain secrets or raw device-supplied strings that have not been sanitized. Adding an event name or field is a contract change (bump the header version below).

Contract version: **1**
