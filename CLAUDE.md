# CLAUDE.md: shared rules for every Claude Code session

Project: edge agentic IoT framework (smart home). Read `docs/ARCHITECTURE.md` for the plan and `contracts/` for the interfaces.

## Rules

1. **Read `contracts/` before touching anything.** Services talk only through contracts; no service imports another's code.
2. **Never change a contract without updating it and bumping its schema/version** (`announce.schema.json` `schema` const, `openapi` `info.version`, doc version headers). Update `contracts/samples/` and run `tools\contract-test.ps1`.
3. **Every service has a `--mock` mode** so it builds and tests on the PC.
4. **No secrets in git or logs.** No certs, keys, passwords, tokens. Generated material goes to git-ignored paths (`deploy/mosquitto/certs/`, `.env`).
5. **Code must run on ARM64 Linux without a GPU**, except `yolo-node` (Jetson, TensorRT).
6. **Device-supplied strings are untrusted.** Length-limit and sanitize; never place them raw in an LLM prompt. Names/rooms/aliases are admin-set labels only.
7. **Label estimates vs measurements** in docs and reports: `[measured]` vs `[est]`.
8. **Mosquitto dynsec user/role management from services must use the dynsec MQTT API** (`$CONTROL/dynamic-security/v1` over TLS), never process arguments (`mosquitto_ctrl -P ...`, `docker exec`): arguments leak into process lists and logs. The `deploy/mosquitto/*.ps1` helpers are dev-PC test tools only.
9. `risk: confirm` actions need a gateway-minted `confirmation_token`, attached by the agent executor out-of-band; it must never appear in a tool schema, prompt, log or event.
10. The LLM has exactly 5 runtime tools. Nothing the LLM can call may add, remove or modify devices or risk levels.

## Environment: Windows 11 + PowerShell 5.1

- Dev PC is Windows; shell is PowerShell 5.1 (no `&&`, no `rsync`, no Makefile). Use PowerShell scripts in `tools\`.
- Python packages go in a local `.venv` only; never install globally.
- Target nodes (Pi 5, Jetson Nano) are **Linux**: service code stays bash/Python/Docker.
- Deploy with `tools\deploy.ps1` (ssh/scp). Check nodes with `tools\node-check.ps1`.

## Worktree workflow

- Each session works in its own git worktree (e.g. `edge-agent-s0` on branch `s0-contracts`), only inside that folder.
- Never switch branches, never touch `main`, never push or merge unless the owner asks.
- Small logical commits. Do not edit `docs/ARCHITECTURE.md` unless the owner says so.
- Session brief: "Read CLAUDE.md and contracts/. Build only `<component>`. Provide a mock mode, tests and a README with run steps."
