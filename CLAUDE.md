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
8. **Mosquitto dynsec user/role management from services must use the dynsec MQTT API** (`$CONTROL/dynamic-security/v1` over TLS), never process arguments (`mosquitto_ctrl -P ...`, `docker exec`): arguments leak into process lists and logs. The `deploy/mosquitto/*.ps1` helpers are dev-PC test tools only. Creating node users must be **idempotent**: if the client already exists, set its password and ensure its role (never keep the old password, never fail on "already exists").
9. `risk: confirm` actions need a gateway-minted `confirmation_token`, attached by the agent executor out-of-band; it must never appear in a tool schema, prompt, log or event.
10. **Service-to-service secrets: one per calling service, scoped to its operations.** device-hub's secret authorizes the gateway's `POST /internal/confirmations/consume` only; the agent has no secret for the gateway. Calls INTO device-hub are authenticated too (`contracts/openapi/device-hub-internal.yaml`): the gateway secret allows everything, the agent secret only the five runtime tools via `POST /mcp` plus the read-only `GET /events` limited to a server-side allowlist of event types (no provisioning, admin or credential-bearing event). The gateway's secret for the agent's local API (`openapi/agent-internal.yaml`) is a third, separate file, `agent-api.secret`; the agent holds only its verifier and never uses it for the hub. Files are `/etc/edge-agent/secrets/<service>.secret`, mode 600, owned by that service's user; read from the file, never from env/argv, never logged (`deploy/README.md`).
11. The LLM has exactly 5 runtime tools. Nothing the LLM can call may add, remove or modify devices or risk levels.

## Environment: Windows 11 + PowerShell 5.1

- Dev PC is Windows; shell is PowerShell 5.1 (no `&&`, no `rsync`, no Makefile). Use PowerShell scripts in `tools\`.
- Python packages go in a local `.venv` only; never install globally.
- Target nodes (Pi 5, Jetson Nano) are **Linux**: service code stays bash/Python/Docker.
- Deploy with `tools\deploy.ps1` (ssh/scp). Check nodes with `tools\node-check.ps1`.

## Parallel sessions (several sessions share this PC)

- **Never kill processes by name**: no `taskkill /IM`, `Stop-Process -Name`, `pkill`, `killall`. Stop only a PID you started yourself in this session (record it when you launch the process).
- **Never touch containers (or networks, volumes) you did not start.** No `docker rm/stop/kill` on others, no `docker system prune`, no `docker compose down` outside your own project.
- **Any dev broker or compose stack uses a unique compose project name** (`-p <session>-<random>`, e.g. `s1-ab12`) **and a free host port** (check it is free, or let Docker pick an ephemeral one and read it back). Never reuse a fixed container name or the default port 8883 for tests.
- Tear down only what you started, by its project name or PID.

## Worktree workflow

- Each session works in its own git worktree (e.g. `edge-agent-s0` on branch `s0-contracts`), only inside that folder.
- Never switch branches, never touch `main`, never push or merge unless the owner asks.
- Small logical commits. Do not edit `docs/ARCHITECTURE.md` unless the owner says so.
- Session brief: "Read CLAUDE.md and contracts/. Build only `<component>`. Provide a mock mode, tests and a README with run steps."

Windows PS 5.1 rules: native-command stderr (docker, pip, git) becomes a terminating error under $ErrorActionPreference='Stop'; wrap native calls with 'Continue' and check $LASTEXITCODE. Tee-Object writes UTF-16; use Out-File -Encoding utf8. $PSScriptRoot is empty in param() defaults.
