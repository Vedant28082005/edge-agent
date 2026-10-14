# agent (S3) — agent v1

HTTP on `127.0.0.1:8200` (loopback only). Event bus, registry-built prompt with a hard token budget and a stable cached
prefix, planner + executor split, constrained decoding from the live registry, confirmation flow, SSE chat, SQLite
persistence. **Not here (S8):** fast path, cost-based scheduler, memory extraction, vision/voice (`audio_ref` and
`image_refs` are refused with 400). Read first: `CLAUDE.md`, `contracts/mcp-tools.md` (v5), `contracts/agent-events.md` (v6),
`contracts/openapi/device-hub-internal.yaml` (1.1.0).

## Run (Windows, PowerShell 5.1)

```powershell
cd services\agent
py -3.12 -m venv .venv                         # local venv only (target is Python 3.11 on ARM64, pure-Python deps)
.\.venv\Scripts\python -m pip install -r requirements-dev.txt

.\.venv\Scripts\python -m agent --mock         # scripted LLM + fake hub on 127.0.0.1, no network
curl.exe -N -X POST http://127.0.0.1:8200/v1/chat -H "Content-Type: application/json" `
  -d '{\"user_id\":\"alice\",\"session_id\":\"s1\",\"text\":\"turn on the bedroom fan\"}'

.\.venv\Scripts\python -m pytest -m "not llama"        # everything, incl. the real-hub integration test
.\tools\run-tests.ps1                                    # same
```

Real mode: `python -m agent --config agent.json` (keys in `agent/config.py`). The hub secret is read **from a file**
(`hub_secret_file`, default `/etc/edge-agent/secrets/agent.secret`, mode 600), never from env/argv, and is registered
with the log redactor. Runtime switch for constrained decoding: `--constrained on|off`, or `POST /v1/runtime
{"constrained": false}` (S10 ablation); `GET /v1/stats` returns counters (plans, calls rejected by code, executed).

## Design

| Piece | File | Notes |
|---|---|---|
| Hub access | `hub_client.py` | JSON-RPC client for `POST /mcp` (only the 5 tools; anything else raises), SSE consumer for `GET /events` (allowlisted types, no `state`), jittered backoff, `Last-Event-ID`, `hello` version check, hub-restart detection |
| Event bus | `events.py` | every input is an `Event` (`user_message`, `hub.*`, `perception`, `node_health`); handlers run as tasks; stub `RulesEngine` over an empty `rules` table |
| Registry cache | `devices.py` | `list_devices` parsed and sanitised; reloaded on `registry_changed`/`node_status`/hello mismatch |
| Prompt | `prompt.py` | prefix = system + tool text + device list (`id|name|room|type|tags`, no live state), rebuilt only when the registry content changes; tail = history, summary, memories, rooms detail, current text. Budgets [est]: 300/300/400/150/400/100 (+300 tool results), enforced by truncation and re-checked by `Prompt.check()`. Over-budget device list degrades to a rooms summary, ids of the rooms the user names go in the tail |
| Constrained decoding | `schema.py`, `llm.py` | see below |
| Planner / executor | `planner.py`, `executor.py` | planner returns `{calls:[…≤4], say}`; the executor validates each call against the live registry, applies the policy, calls the hub, logs |
| Policy | `policy.py` | `needs_confirmation(action, trigger)`: risk:confirm + user → CONFIRM, + rule/perception/proactive → NOTIFY_ONLY (never prompt, never execute), + guest → DENY |
| Confirmation | `confirmations.py` | emits `confirmation_request`, waits (60 s), `declined` / `expired` / token → executor re-issues with the token in `_meta` only |
| Settle | `settle.py` | `expect(call_id)` before the call, `arm` after; hub `action_settled` / `action_settle_timeout` matched by `call_id`, forwarded on the originating stream (open after `final` until settled, max 150 s); unmatched timeouts → notifier |
| Chat | `chat.py`, `app.py` | `POST /v1/chat` SSE: `status, tool_call, tool_result, token, final, confirmation_*, action_settled, action_settle_timeout, error` (+ `seq`, `req_id`) |
| Store | `store.py` | SQLite: `messages`, `summaries`, `action_log`, `rules`; per-user filtering on every query; guests: in-process only |

**Constrained decoding approach.** llama.cpp's `response_format = {"type":"json_schema", …}` is compiled to a GBNF grammar
and enforced per token. We constrain the planner's *whole reply* (`{"calls": [...], "say": "..."}`) instead of using
llama.cpp's native `tools` mode: native tool calls depend on the chat template and on lazily-triggered grammars and cannot
guarantee an enum of endpoint ids. The schema is generated from the live registry: one `set_state` variant per
(capability, value-schema) with an `enum` of the endpoints that have it, so a non-existent endpoint, a capability the
endpoint lacks, a wrong type or an out-of-range value cannot be expressed (`tests/test_schema.py` checks this, and checks
the schema and the executor's validator agree on a grid). Unconstrained mode sends no schema; the executor's validation is
always on, so the hardware is protected either way and the invalid calls are counted. The exact `response_format` shape
and `id_slot`/`cache_prompt` fields follow current llama-server and were **not** exercised against a real server here.

## Tests (what ran)

`pytest -m "not llama"`: 98 tests — prompt budget under adversarial input, prefix stability (history changes → identical
bytes; registry change → different; online/live state → identical), schema generation, executor budgets
(steps/calls/deadline/retries), confirmation flow (approve/decline/expire/token_expired/wrong user/guest/rule), settle
matching (incl. early arrival, two streams, detached client), event-bus dispatch, stream reconnect + `Last-Event-ID`,
untrusted-string handling, no secret/token in logs/events/store/model prompts, store isolation, llama request shape,
mock end-to-end (HTTP SSE), and `test_integration_hub.py` against the **real S1 device-hub** (`--mock` internals, caller
auth ON, agent secret scope enforced, hub-side token consumption, our action hash equals the hub's).
`tests/test_llama_real.py` (needs `EDGE_TEST_GGUF`, optional `EDGE_LLAMA_SERVER`) is written, **not run**.

## Decisions for the owner (contract gaps; nothing in `contracts/` or `docs/` was edited)

1. **How the agent receives the confirmation token is not defined.** `agent-events.md` says the gateway "hands it to the
   agent" but no agent endpoint/auth exists. Built: everything up to `ConfirmationManager.resolve(request_id, user_id,
   approved, token)` (in-process). Not built: any network path for it, so in real mode every request ends in
   `confirmation_expired` (fail closed). Same gap for decline and for **Retry** (`/confirmations/{id}/retry` needs a
   gateway->agent call plus a stream to emit the new request on). `--mock` has a mock-only `POST /mock/confirm`.
2. **`run_scene` cannot be hashed or discovered by the agent.** No tool lists scenes (the LLM can only guess a name; the
   optional `scene_names` config pins an enum), and the blocked result has no `scene_id`, so `confirmation_request.action_hash`
   is `null` for scenes (set_state hashes are computed with RFC 8785 and match the hub's, tested).
3. **Settle events:** the brief says `async_result`; `agent-events.md` v6 says `action_settled` / `action_settle_timeout`
   on the originating stream (`async_result.kind` is only `vision`). I followed the contract.
4. **Notifications** (blocked proactive actions, late timeouts) are specified as gateway-WebSocket messages, but there is no
   agent->gateway channel; v1 logs them and keeps the last 100 in memory (`notify.py`).
5. **Guest identity:** the chat request has no role, so guests are recognised by `user_id == "guest"` or `guest:*`.
   Suggest an explicit field (contract change) or gateway-side rewrite.
6. **`node_status` has a node id but `list_devices` has none**, so `online` is refreshed by reloading `list_devices`
   (prefix unaffected: `online` is not in it).
7. **Model sees capability ids per type** (`fan=power,speed`) in the device-list section; ranges are enforced only by the
   grammar/executor. This goes slightly beyond "names, rooms, types, tags".
8. **S1 observation:** `device_hub --mock` ignores `auth.verifier_dir` from `--config` (it builds its own `HubConfig`) while
   `__main__` still validates the config's directory, so mock + caller auth is unusable from the CLI; the integration test
   drives `build_mock_hub` + `create_app` directly. Worth a fix in S1.
9. Extra endpoints not in the contract: `GET /health`, `GET /v1/stats`, `GET|POST /v1/runtime` (loopback eval switches).
