# deploy/

Broker stack for `pi-brain` (Mosquitto 2.x, TLS on 8883, dynamic-security). Run PowerShell scripts from the repo root on the Windows dev PC.

## First-time setup (dev PC)

```powershell
# 1. private CA + broker certificate (SANs: localhost, 127.0.0.1, your LAN IP, tailnet name)
deploy\mosquitto\new-certs.ps1 -LanIp 192.168.1.10 -TailnetName pi-brain.<tailnet>.ts.net -ExtraDns mosquitto   # SAN 'mosquitto' = the name the hub container uses; setup-pi-brain.sh refuses a cert without it
# 2. dynsec config with the hub admin (random password -> deploy\mosquitto\.env, git-ignored)
deploy\mosquitto\dynsec-bootstrap.ps1
# 3. start the broker (dev: localhost only)
cd deploy; docker compose -f compose.yml -f compose.dev.yml up -d mosquitto     # broker only
# 4. prove node isolation
cd ..; deploy\mosquitto\acl-test.ps1
```

All generated material (`certs/`, `data/`, `.env`) is git-ignored. `certs\ca\ca.key` never leaves the PC. On the PC, `new-certs.ps1` restricts the key files to your Windows user.

## Stack on pi-brain: images, compose, llama-server

`compose.yml` runs mosquitto + **device-hub, gateway, agent** from images built by `tools\build-images.ps1` (python:3.11-slim-bookworm, pinned wheels in `docker/requirements-*.lock`, `--only-binary=:all:`, fixed uids 10101/10102/10103, read-only root fs + tmpfs, one data volume each under `/var/lib/edge-agent/<service>`, json-file log rotation, `mem_limit` per service [est]). One private network `edge`; **only the gateway (`127.0.0.1:8000`, fronted by `tailscale serve`, see `tools/tailscale-serve.md`) and mosquitto (8883) publish ports**. device-hub 8100, agent 8200 and the gateway's internal consume listener 8001 are not published. Each service mounts only its own secret files (table below); `tools\compose-test.ps1` enforces both rules.

```powershell
tools\build-images.ps1 -Tag v1 -Save -Smoke         # buildx arm64 (emulated smoke) + amd64; tarballs in deploy\dist\ (git-ignored)
tools\deploy.ps1 -Target brain -Images -Tag v1 -Up  # copy non-secret files + tarballs, docker load, compose up (-DryRun previews)
tools\e2e-smoke.ps1                                # local amd64 end-to-end on a throwaway compose project
```

The LLM is **not** in compose: `systemd/llama-server.service` runs the native llama-server (one slot, `--temp 0`, context/threads/model from `/etc/edge-agent/llama-server.env`, no model shipped). It binds the edge bridge address (`LLAMA_HOST`, default `172.29.0.1`; containers reach it as `host.docker.internal`, the LAN cannot). First admin, once: `docker compose run --rm -it gateway --config /etc/edge-agent/config/gateway.json bootstrap-admin --username NAME`. Images carry no secret (`python tools/secret_scan.py image ...` scans every layer).

## One-time Pi setup (secrets, users, dirs, config, llama unit, chrony)

Routine `tools\deploy.ps1 -Target brain` needs **no root** and carries **no secrets**: it copies only `contracts/`, `compose.yml`, `mosquitto.conf`, the role template, the chrony config, `deploy/config` (non-secret JSON), `deploy/systemd` (llama unit + env template) and `tools/setup-pi-brain.sh`, plus the image tarballs with `-Images`. Secrets reach the Pi once, by hand:

```powershell
tools\deploy.ps1 -Target brain                      # puts setup-pi-brain.sh and the chrony config on the Pi
ssh pi@pi-brain "mkdir -p -m 700 ~/edge-agent-setup"
scp deploy\mosquitto\certs\ca.crt deploy\mosquitto\certs\server.crt deploy\mosquitto\certs\server.key deploy\mosquitto\data\dynamic-security.json pi@pi-brain:edge-agent-setup/
# also stage the hub MQTT admin password (HUB_ADMIN_PASSWORD from deploy\mosquitto\.env) as a file named hub-mqtt.password
ssh -t pi@pi-brain "LAN_IP=192.168.1.10 LAN_CIDR=192.168.1.0/24 bash ~/edge-agent/tools/setup-pi-brain.sh"   # asks for your sudo password
tools\deploy.ps1 -Target brain -Up                  # starts the broker
```

`setup-pi-brain.sh` checks that the key matches the cert and chains to the CA, installs everything under `/etc/edge-agent/mosquitto` owned by uid 1883 (the container user; `server.key` and `dynamic-security.json` mode 600), points compose at it through `deploy/.env` (`MOSQUITTO_STATE_DIR`), creates the per-service secret files (below), installs chrony with the edge-agent config, and shreds the staged secrets. It is safe to re-run (stage new files first) after renewing the server cert or changing dynsec. Needs an interactive sudo password, not passwordless sudo.

## Service secrets (internal APIs)

Both internal APIs authenticate callers with `X-Service-Secret` (loopback or unix socket only): the gateway's `/internal/*` (`gateway.yaml`) and device-hub's API (`device-hub-internal.yaml`, auth is mandatory; the only exception is the `--insecure-no-auth` dev flag, accepted only with `--mock`). **One secret per calling service**, each scoped to the operations that service needs:

| Caller | Secret file (mode 600, owned by that service's user) | Called service | Authorizes |
|---|---|---|---|
| device-hub | `/etc/edge-agent/secrets/device-hub.secret` (owner `edge-device-hub`) | gateway | `POST /internal/confirmations/consume` only |
| gateway | `/etc/edge-agent/secrets/gateway.secret` (owner `edge-gateway`) | device-hub | everything (scope `gateway`) |
| agent | `/etc/edge-agent/secrets/agent.secret` (owner `edge-agent`) | device-hub | the five runtime tools via `POST /mcp` only (scope `agent`) |
| gateway | `/etc/edge-agent/secrets/agent-api.secret` (owner `edge-gateway`) | agent local API | everything in `agent-internal.yaml`; the agent holds only `agent-verifiers/agent-api.sha256` (owner `edge-agent`) |

The hub's own MQTT account password is `hub-mqtt.password` (owner `edge-device-hub`). Verifier directories: `verifiers/` (gateway), `hub-verifiers/` (device-hub), `agent-verifiers/` (agent); each service mounts only its own.

The agent has **no** secret for the gateway: it cannot mint or consume confirmation tokens.

`setup-pi-brain.sh` creates the system users, generates each secret (256-bit, `openssl rand`), and writes a SHA-256 **verifier** that only the called service can read: `verifiers/device-hub.sha256` (owned by the gateway user) and `hub-verifiers/gateway.sha256`, `hub-verifiers/agent.sha256` (owned by the device-hub user), all mode 600 under `/etc/edge-agent/secrets/`. The called service checks `X-Service-Secret` against the verifier in constant time, so it can authenticate callers without being able to impersonate them (device-hub reads only verifiers, never a secret), and a compromised service leaks only its own scope. Services read their own secret from its file at start-up (never from env vars or argv) and never log it. Containers for device-hub, the gateway and the agent must run as the matching uid. Rotate with `ROTATE_SECRETS=1` and restart the affected services. The secrets are generated on the Pi and are never copied from or to the PC or git.

## Certificate lifecycle

- **CA**: 10 years (3650 days). **Server certificate**: 3 years by default (`-ServerDays`), always issued by the same CA. Both certificates are generated with `notBefore` one day in the past so a slightly slow clock on the Pi or a board does not make them "not yet valid" (needs OpenSSL 3.4+; Git for Windows 2.50+ has it).
- **Before the first Pi deploy, regenerate with the real values** (the dev run used placeholders): `deploy\mosquitto
ew-certs.ps1 -LanIp <pi-brain static LAN IP> -TailnetName <pi-brain>.<tailnet>.ts.net -Force`. The broker's LAN IP goes into every board's provisioning bundle, so give `pi-brain` a static address or DHCP reservation; a changed IP means a new server cert **and** new bundles.
- **Back up `certs\ca\ca.key` offline** (encrypted USB or password manager attachment, not cloud sync, not git). It is the root of trust for every board; losing it means you cannot issue a new server cert under the same CA. It never goes to the Pi.
- **Renewing the server cert** (same CA): rerun `new-certs.ps1 -LanIp ... -TailnetName ... -Force`, stage the new files and rerun `tools/setup-pi-brain.sh` (see above). Boards keep working, they trust the CA.
- **Rotating or replacing the CA means re-provisioning every board**: the CA certificate is stored in each board's NVS from the BLE bundle, so a new CA is only trusted after each board is provisioned again. Treat the CA key as permanent; rotate only if it is compromised.
- Boards verify the certificate dates, so they need a plausible clock (see NTP below).

## Files

| File | Purpose |
|---|---|
| `compose.yml` | mosquitto + device-hub + gateway + agent (production, ARM64) |
| `compose.dev.yml` | Overlay: broker on 127.0.0.1, no auto-restart (use `up -d mosquitto`) |
| `docker/` | Dockerfile per service, pinned `requirements-*.lock`, `healthcheck.py` |
| `config/` | non-secret service JSON (`device-hub.json.template`, `gateway.json`, `agent.json`) |
| `systemd/` | `llama-server.service` + `llama-server.env.template` |
| `mosquitto/mosquitto.conf` | TLS 8883 only, dynsec plugin, anonymous off |
| `mosquitto/new-certs.ps1` | CA + server cert; SANs from parameters |
| `mosquitto/dynsec-bootstrap.ps1` | Initial dynsec config, hub admin, default-deny ACL |
| `mosquitto/node-role.template.json` | Role limiting a node to `home/v1/nodes/<node_id>/#` |
| `mosquitto/dynsec-node-user.ps1` | Create/remove a node user + role (testing; the hub does this in production) |
| `mosquitto/acl-test.ps1` | Isolation + revocation proof against a running broker |

Production: `tools\deploy.ps1 -Target brain -Images -Up`. Tests (no hardware): `tools\compose-test.ps1`, `tools\setup-pi-brain-test.ps1`, `tools\deploy-test.ps1`, `tools\e2e-smoke.ps1`.
ESP32s need the CA cert (delivered in the BLE bundle). Their TLS check needs a valid clock (NTP) and the broker's LAN IP in the SANs.

## NTP (needed for ESP32 TLS)

The ESP32 must know the time before it can verify the broker certificate. The Pi 5 has an RTC but no battery by default, so:

1. Install `chrony` on `pi-brain` and use `chrony/chrony-pi-brain.conf` (syncs from `pool.ntp.org`, serves your LAN, keeps serving a free-running clock with `local stratum 10` if the internet is down). Open UDP 123 from the LAN.
2. The BLE provisioning bundle carries `ntp_host` = the Pi's LAN IP (same address as the broker). The firmware tries `ntp_host` first and falls back to `pool.ntp.org`.
3. **Keep `fake-hwclock` enabled** (it is on by default in Raspberry Pi OS). It saves the time periodically and restores it at boot, so after a power cut with no internet the Pi starts from its last known time instead of the 1970/image-build date. Do not disable or remove it.
4. **RTC backup battery: recommended, not required.** The Pi 5 has an RTC but no battery by default; with one, the clock is exact across power cuts, without it `fake-hwclock` gives a "last saved time" that can be minutes to days stale. Fit the official Raspberry Pi RTC battery (rechargeable lithium cell on the J5 connector). **Only enable charging with a rechargeable cell**: add `dtparam=rtc_bbat_vchg=3000000` to `/boot/firmware/config.txt` and reboot, and **never enable it with a non-rechargeable coin cell (e.g. CR2032): charging a primary cell can make it leak, overheat or burst.** Without a battery, or with a non-rechargeable one, leave the setting out. Check with `sudo hwclock -r`. The chrony config has `rtcsync` so the RTC follows the synced time.
5. **`local stratum 10` is kept** on purpose: if the internet is down the Pi still serves its own clock (from `fake-hwclock` or the RTC) so the LAN stays consistent and boards can still do TLS. It is only as good as the saved/RTC time, which is why the firmware also rejects times before its build timestamp.
6. **Firmware safety net** (required in `contracts/mqtt.md`): boards refuse to treat NTP time earlier than their firmware build timestamp as valid and do not open TLS until the time is plausible, so a stale Pi clock makes boards wait and retry instead of connecting with wrong dates.

Status: documented and configured on paper; not tested on hardware.
