# agent (S3) — agent v1

HTTP on `127.0.0.1:8200` (loopback only). Event bus, registry-built prompt with a hard token budget and a stable cached
prefix, planner + executor split, constrained decoding from the live registry, confirmation flow, SSE chat, SQLite
persistence, and (S8) the scheduler in `agent/sched/` (below). **Not here:** memory extraction / persistent fact store,
voice (S8c adds the real vision-node / yolo-node clients behind `PerceptionBackend`, disabled by default). Read first: `CLAUDE.md`, `contracts/mcp-tools.md` (v5), `contracts/agent-events.md` (v6),
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

## Scheduler (S8, `agent/sched/`)

All of it sits behind the five tools; nothing here can add one. Config: `AgentConfig.sched` (JSON object, `sched/config.py`).

| Piece | File | Notes |
|---|---|---|
| Fast path | `resolver.py`, `text.py` | Deterministic: normalise, intent (on/off, set value, lock, open/close, scene), score every registry device by label/room/type/tag tokens (typo-tolerant), plan only if coverage x intent confidence >= `fast_threshold` (0.85) and the winner beats the runner-up by `fast_margin`; "all lights in X" groups (cap 8). Questions, negation, time/condition, compound and relative phrasing, unknown words ("kitchen fan" with no kitchen fan), out-of-range values fall through to the LLM. The resolver only sees the caller's scoped registry and scene list (`allowed_rooms`/`allowed_scenes`). Guests (S8b rule): `set_state` utterances are denied, scenes fall through. Execution = `Executor.run_calls`, the same path as an LLM plan |
| Router | `router.py` | Perception questions: sensor (`read_sensor`), YOLO jetson/pi, VLM yes/no / short / JSON, LLM last. Cost table in config (`est`/`measured` flags), EWMA of observed latency, failure breaker (30 s), health gate (VLM < 400 MB, node down). Vision sources need a registered `PerceptionBackend` (below; the nodes are disabled by default, so by default they are skipped). A 429 `queue_full` is *busy*: the ladder moves up, no breaker, no EWMA sample |
| Node clients | `nodes.py`, `images.py` | S8c. `VisionNodeClient` (vlm_yesno / vlm_short / vlm_json / `yolo_pi` fallback) and `YoloNodeClient` (`yolo_jetson`: upload or camera snapshot) per `vision-node-internal.yaml` / `yolo-node-internal.yaml` 1.0.0. Multipart upload of the caller's real image (first of up to 4; `InlineImage.ref` is mapped to the stricter `image_ref` pattern, bad characters become `_`, empty becomes `img<n>`), per-node `X-Service-Secret` read from its own file, timeouts, no redirects. Image bytes live in RAM for the turn (`ReqCtx.image_data`, dropped in `ChatService.on_user_message`), are never persisted or logged, and are excluded from `repr`. **All node output is untrusted**: responses are re-validated by hand against the closed contract schemas (any unknown field, bad type, range or character class drops the whole response and counts as a failure); YOLO answers are built in the client from validated detections using only COCO class names, never node text; `vlm_yesno` is `yes/no/unsure`; short/JSON text is shown as a quoted, labelled "unverified" data block. EWMA input = the node's `latency_ms - queue_ms` (round trip for cached answers). Snapshots (`/v1/detect/snapshot`, no image on the wire) only for admin/member, only for a configured camera whose room is in the caller's scope, never for guests; the question must name the room if more than one camera exists; any doubt means no snapshot |
| Constrained decoding | `grammar.py`, `llm.py` | `schema.build_plan_schema` -> `schema_to_gbnf`; `ConstraintCache` keyed on the hub's `registry_version` from `list_devices` (content hash of ids/capabilities/ranges/enums/rooms/scenes when the hub sends none), one entry per request scope; backends `json_schema` (response_format) or `gbnf` (grammar field) |
| Prefix cache | `prefix.py`, `prompt.py` | Static prefix first, then history/summary/memories/current; `cache_prompt` + pinned `id_slot`; `PrefixTracker` records same-prefix and server `cache_n` per turn |
| Memory | `memory.py` | Per-user window by token budget (+ the builder's 4-message cap), rolling summary with its own budget, deterministic summariser (hook for an idle LLM one); guests in-process only |
| Trace | `trace.py`, `scheduler.py` | One per turn: route, candidates, latency, tokens, cache hit, constrained, outcome, flags. Bus event `sched.trace` + a row in the `turn_traces` table (own retention cap, never in the action log); no user text. SSE `route` stays `fast|llm|vision` (sensor answers are `fast`) |

Perception nodes (`sched.nodes`, **all disabled by default**; unknown keys are rejected at startup):

```json
{"sched": {"nodes": {
  "vision": {"enabled": true, "base_url": "http://pi-vision:8300",
             "secret_file": "/etc/edge-agent/secrets/vision-node-api.secret", "timeout_s": 30},
  "yolo":   {"enabled": true, "base_url": "http://jetson-yolo:8400",
             "secret_file": "/etc/edge-agent/secrets/yolo-node-api.secret", "timeout_s": 10,
             "snapshot_sources": [{"source_id": "cam0", "room": "Hall"}]}}}}
```

`base_url` is the node's Tailscale name (plain HTTP over WireGuard per the contracts; `https` + `ca_file` also accepted).
`secret_file` is a path: one secret per node, mode 600, owned by the agent user, never in env/argv/logs. An enabled node whose
secret cannot be read is skipped (logged by node name only). `min_conf` (0.05..1, default 0.25) sets the detection threshold.
Tests: `tests/test_sched_nodes.py` (mock node servers answering with `contracts/samples/*`).

Ablation: `fast_path`, `router`, `constrained_decoding`, `prefix_cache` in `sched` config or live with `POST /v1/runtime`
(`GET /v1/stats` has per-route counters and the cost table). Benchmark (mock LLM, simulated latency, prints per-route
latency per ablation): `.	oolsench-sched.ps1`. Real constrained-decoding gate: `EDGE_TEST_GGUF` + `tests/test_llama_real.py`
(both backends; **not run**).

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
4. Images are held in RAM for the turn and handed to the vision backends when `sched.nodes` enables one; with no node enabled they are accepted and discarded as before. A turn with only images and no text still answers "I can't analyse images yet."
5. Extras beyond the contract, authenticated: `GET /v1/stats`, `GET|POST /v1/runtime` (constrained on/off for S10).
6. Capability ids per type are shown to the model in the device list (slightly beyond names/rooms/types/tags).
7. `node_status` has a node id but `list_devices` has none, so `online` is refreshed by reloading `list_devices`.
8. S1 observation: `device_hub --mock` ignores `auth.verifier_dir` from `--config`; the integration test builds the hub in code.


## Agent 1.3.0: request scope, registry_version, scenes, route enum

* **Scope** (`scope.py`): `POST /v1/chat` carries `allowed_rooms` / `allowed_scenes`. Missing = `[]` for member and guest (nothing allowed);
  explicit `null` = unrestricted for admin/member, never for guest (becomes `[]`); malformed = `[]`. The executor refuses
  `set_state`, `read_sensor`, `get_state`, `list_devices(room)` outside `allowed_rooms` and `run_scene` outside `allowed_scenes` with
  `{"ok": false, "error": "out_of_scope"}` (action-log status `out_of_scope`) before anything reaches the hub, on the LLM, fast-path,
  rule and approved-retry paths. `list_devices` results, the prompt device/scene list, the decoding schema, the fast-path resolver and
  the sensor router all use a per-scope filtered registry view (`views.py`), so out-of-scope names never reach the model. Unrestricted
  callers keep the shared byte-stable prompt prefix.
* **Guests** (S8b rule kept): never `set_state` / `run_scene`, even in an assigned room (`not_allowed`, `denied_guest`). They may read
  (`get_state`, `read_sensor`, `list_devices`) inside `allowed_rooms`.
* **Approved Retry the agent never saw**: the confirmation body has no scope, so it runs with the same user's latest chat scope (same role,
  at most 30 min old), else with nothing allowed.
* **registry_version / scenes**: `list_devices` returns `registry_version` and a read-only `scenes` list; the grammar cache is keyed on the
  version (content hash when absent), scene names come from the list, the `scene_names` config key is retired (ignored if present).
* **Routes**: SSE `final.route` is `fast|llm|sensor|yolo|vlm`; `vision` is legacy and never emitted. Traces carry the same values.

## Route mode and reply policy (S14)

`sched.route_mode` (default `hybrid`) decides who answers first: `hybrid` = the no-LLM fast path only for `sched.fast_intents`
(default `command`, `set_value`, `compound`; others: `relative`, `status`, `list`, `identity`, `ambient`), `llm_first` = no fast
path, `rules_first` = every fast intent (pre-S14 behaviour, no model `say`). Outside `rules_first` the model's JSON array may carry one
short string (`say_max_tokens`, default 25) beside or instead of its calls, and a fact line (assistant name from `assistant_name`,
model name from `llm_model` or `/props`, device types) lets it answer identity questions. The agent still composes the verified text from
the tool results; the model's `say` replaces it only if `reply.say_verdict` finds no contradiction (no unrun action, no state claimed
`sched.say_in_plan` (default `false`) lets the plan array carry a `say` string; per-model switch. Any `say` must also be a clean sentence (>=3 words, no brackets/quotes, no bare alias or tool code, terminal punctuation) or the agent text / capability line is used; a hello/thanks turn never runs a read.
without a read, numbers and on/off/locked/open polarity match). `sched.natural_read_replies` adds a second short LLM call that phrases
read-only answers from the read results and streams it as `token` events (checked by `reply.phrase_verdict`, else the agent text).
Each trace records `route_mode`, `text_source` (`model`|`agent`) and `say_verdict`.
