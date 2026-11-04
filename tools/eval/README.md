# S15 evaluation harness

Runs ~45 phrases (commands, relative "it", ambiguous, room/group, reads, chat, comfort, unknown device, partial, guest, injection) against a
**live agent through the gateway**, from the PC, and writes a CSV: `config, id, step, category, role, phrase, route, decision, tools_called,
correct, wrong_device, latency_ms, server_latency_ms, prompt_eval_tokens, cache_hit_tokens, llm_calls, reply, notes, config_flags, req_id`.

* `phrases.json` - the cases. Placeholders (`{fan}`, `{light_unique}`, `{room}` ...) are filled with the REAL device names read from
  `GET /devices`; a case the registry cannot support is skipped with the reason. Expected result per step: the writes that must be executed
  (a device, or a group such as "everything in the room"), `no_action`, a required read, a reply regex, a forbidden reply regex, and whether a
  confirmation request is expected/forbidden.
* `correct` = every expectation met and nothing else written. `wrong_device` = an executed write on a device the phrase did not name (for a
  no-action phrase, any executed write). An extra `power: true` on the right device (the generic power-dependency rule) is allowed.
* `latency_ms` is measured at the PC from sending the request to the `final` event; `server_latency_ms` is the agent's own number.
  `prompt_eval_tokens` / `cache_hit_tokens` / `decision` / `config_flags` come from the agent's turn trace, matched by `req_id`
  (`tools\dump-traces.ps1`).
* Before each case the harness puts every device the previous case touched back (power off, cover closed) through the gateway's
  `POST /devices/{id}/set`, then applies the case's setup. **risk:confirm devices (locks) are never touched.** A phrase that could raise a
  confirmation request for one is listed and only sent after you type `SEND`; the harness **declines** every confirmation request, never approves.
* Credentials: a password prompt (or `EDGE_EVAL_PASSWORD` / `EDGE_EVAL_GUEST_PASSWORD` for an unattended run). Nothing is stored or printed.

## The three configurations

| label | `sched` keys (merge into `/etc/edge-agent/config/agent.json` on the Pi) |
|---|---|
| `baseline` | the four S15 keys absent or false (`configs/baseline.json`) |
| `new` | `deterministic_fixes`, `generated_prompt`, `chat_route` true (`configs/new.json`) |
| `compact` | `new` + `compact_json` true (forces the gbnf constraint backend; `configs/compact.json`) |

Flip, restart, then run with the same label: `ssh pi@pi-brain "cd ~/edge-agent/deploy && docker compose restart agent"`. The merge step
reads the flags each turn really ran with from the trace (`config_flags`) and warns when they do not match the label.

```powershell
.\tools\eval\run-eval.ps1 -Label baseline -BaseUrl https://pi-brain.YOURTAILNET.ts.net -User alice -GuestUser guest
.\services\agent\.venv\Scripts\python.exe tools\eval\run_eval.py compare eval-results\eval-baseline-*.csv eval-results\eval-new-*.csv eval-results\eval-compact-*.csv
```

`compare` prints one line per configuration (phrases, correct, wrong-device, mean latency, mean tokens evaluated per LLM turn, mean cache hits)
and the phrases whose result differs between configurations.
