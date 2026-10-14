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

.\.venv\Scripts\python -m agent --mock --insecure-no-auth   # scripted LLM + fake hub on 127.0.0.1, no network
curl.exe -N -X POST http://127.0.0.1:8200/v1/chat -H "Content-Type: application/json" `
  -d '{\"user_id\":\"alice\",\"role\":\"member\",\"session_id\":\"s1\",\"text\":\"turn on the bedroom fan\"}'
# without --insecure-no-auth the mock writes a gateway secret + verifier to a temp dir (path in the log); send it as X-Service-Secret

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

`pytest -m "not llama"`: 120 tests (Windows) + `test_api_contract.py` covers the gateway API: auth, role, hash check, hand-off, ring, memory; — prompt budget under adversarial input, prefix stability (history changes → identical
bytes; registry change → different; online/live state → identical), schema generation, executor budgets
(steps/calls/deadline/retries), confirmation flow (approve/decline/expire/token_expired/wrong user/guest/rule), settle
matching (incl. early arrival, two streams, detached client), event-bus dispatch, stream reconnect + `Last-Event-ID`,
untrusted-string handling, no secret/token in logs/events/store/model prompts, store isolation, llama request shape,
mock end-to-end (HTTP SSE), and `test_integration_hub.py` against the **real device-hub** (`services/device-hub`, merged S1, scene flow included) (`--mock` internals, caller
auth ON, agent secret scope enforced, hub-side token consumption, our action hash equals the hub's).
`tests/test_llama_real.py` (needs `EDGE_TEST_GGUF`, optional `EDGE_LLAMA_SERVER`) is written, **not run**.

## Gateway API (contracts/openapi/agent-internal.yaml 1.2.0)

Auth is mandatory on every operation, `/v1/health` included: `X-Service-Secret` is checked in constant time against a
SHA-256 verifier file (`api_verifier_file`, default `/etc/edge-agent/secrets/verifiers/agent-api.sha256`); the agent cannot
read the secret itself and refuses to start without the file. `--insecure-no-auth` only with `--mock`. This is a different
secret from the hub secret (`agent.secret`); neither works on the other API.

* `role` (`admin|member|guest`) is read only from the authenticated gateway's chat/confirmation body. There is no
  guest-by-user-id rule; a guest gets no persistent history/summary/memories and no `risk: confirm` prompts.
* `POST /v1/confirmations/{request_id}`: the full `action` is hashed (RFC 8785) and compared with `action_hash`, a mismatch is
  409 `action_hash_mismatch` with nothing executed; also compared with the pending request's own hash. Approved: the waiting
  turn continues, or, for a request id the agent never saw (a gateway-side Retry), the exact action runs with the token. Declined /
  expired clear the pending state. Repeats are 202 no-ops; unknown or another user's request is 404. The token lives in memory
  for one call; tests grep logs, events, the action log, messages, the ring and the model prompts for it.
* Scenes: `confirmation_request.action` is `{type:"run_scene", scene_id, actions}` and its hash is the hub's `action_hash`,
  which the agent verifies against its own computation (a mismatch is not offered to the user).
* `GET /v1/events`: ring of up to 2000 events (never fewer than 200 / 10 min), `id`/`Last-Event-ID`, `reset` for an id from a
  previous run or older than the buffer. Contains `notification`s, and `async_result` / `action_settled` /
  `action_settle_timeout` / `confirmation_request` only when their chat stream is gone (client disconnected, stream closed, or an
  approval with no stream), each tagged with `user_id` or `"household"`.
* `/v1/memory` (GET list, PUT, DELETE) per user, other users' ids are 404. Memories (top 5, <=150 tokens) go in the prompt tail.
* `/v1/chat`: images are validated (type, magic bytes, base64, 5 MiB, max 4) and kept out of the prompt; v1 cannot analyse them
  (S8) and says so. `429 llm_busy` + `Retry-After` when the turn queue is full.

## Decisions for the owner

1. **Retry vs. unknown request ids.** The contract says an unknown `request_id` is 404, but also that a Retry request is unknown to
   the agent and is executed from the full action at approval. I run the action for an unknown id on `approved`, and answer 404
   for unknown ids on `declined`/`expired`. The token (hub-validated, user- and hash-bound) is what authorizes it.
2. **Events mirrored only when the chat stream is gone**, to avoid the gateway seeing each settle twice. If the gateway wants
   everything mirrored on `/v1/events`, it is a one-line change in `ReqCtx.emit`.
3. **Outcome of an approval with no stream** is reported as a `notification` level `action_result` (the contract's event list has no
   tool_result/final).
4. Images are accepted and discarded (no vision route until S8).
5. Extras beyond the contract, authenticated: `GET /v1/stats`, `GET|POST /v1/runtime` (constrained on/off for S10).
6. Capability ids per type are shown to the model in the device list (slightly beyond names/rooms/types/tags).
7. `node_status` has a node id but `list_devices` has none, so `online` is refreshed by reloading `list_devices`.
8. S1 observation: `device_hub --mock` ignores `auth.verifier_dir` from `--config`; the integration test builds the hub in code.
