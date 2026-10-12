# deploy/

Broker stack for `pi-brain` (Mosquitto 2.x, TLS on 8883, dynamic-security). Run PowerShell scripts from the repo root on the Windows dev PC.

## First-time setup (dev PC)

```powershell
# 1. private CA + broker certificate (SANs: localhost, 127.0.0.1, your LAN IP, tailnet name)
deploy\mosquitto\new-certs.ps1 -LanIp 192.168.1.10 -TailnetName pi-brain.<tailnet>.ts.net
# 2. dynsec config with the hub admin (random password -> deploy\mosquitto\.env, git-ignored)
deploy\mosquitto\dynsec-bootstrap.ps1
# 3. start the broker (dev: localhost only)
cd deploy; docker compose -f compose.yml -f compose.dev.yml up -d
# 4. prove node isolation
cd ..; deploy\mosquitto\acl-test.ps1
```

All generated material (`certs/`, `data/`, `.env`) is git-ignored. `certs\ca\ca.key` never leaves the PC. On the PC, `new-certs.ps1` restricts the key files to your Windows user.

## One-time Pi setup (secrets and chrony)

Routine `tools\deploy.ps1 -Target brain` needs **no root** and carries **no secrets**: it copies only `contracts/`, `compose.yml`, `mosquitto.conf`, the role template, the chrony config and `tools/setup-pi-brain.sh`. Secrets reach the Pi once, by hand:

```powershell
tools\deploy.ps1 -Target brain                      # puts setup-pi-brain.sh and the chrony config on the Pi
ssh pi@pi-brain "mkdir -p -m 700 ~/edge-agent-setup"
scp deploy\mosquitto\certs\ca.crt deploy\mosquitto\certs\server.crt deploy\mosquitto\certs\server.key deploy\mosquitto\data\dynamic-security.json pi@pi-brain:edge-agent-setup/
ssh -t pi@pi-brain "LAN_CIDR=192.168.1.0/24 bash ~/edge-agent/tools/setup-pi-brain.sh"   # asks for your sudo password
tools\deploy.ps1 -Target brain -Up                  # starts the broker
```

`setup-pi-brain.sh` checks that the key matches the cert and chains to the CA, installs everything under `/etc/edge-agent/mosquitto` owned by uid 1883 (the container user; `server.key` and `dynamic-security.json` mode 600), points compose at it through `deploy/.env` (`MOSQUITTO_STATE_DIR`), installs chrony with the edge-agent config, and shreds the staged secrets. It is safe to re-run (stage new files first) after renewing the server cert or changing dynsec. Needs an interactive sudo password, not passwordless sudo.

## Certificate lifecycle

- **CA**: 10 years (3650 days). **Server certificate**: 3 years by default (`-ServerDays`), always issued by the same CA.
- **Before the first Pi deploy, regenerate with the real values** (the dev run used placeholders): `deploy\mosquitto
ew-certs.ps1 -LanIp <pi-brain static LAN IP> -TailnetName <pi-brain>.<tailnet>.ts.net -Force`. The broker's LAN IP goes into every board's provisioning bundle, so give `pi-brain` a static address or DHCP reservation; a changed IP means a new server cert **and** new bundles.
- **Back up `certs\ca\ca.key` offline** (encrypted USB or password manager attachment, not cloud sync, not git). It is the root of trust for every board; losing it means you cannot issue a new server cert under the same CA. It never goes to the Pi.
- **Renewing the server cert** (same CA): rerun `new-certs.ps1 -LanIp ... -TailnetName ... -Force`, stage the new files and rerun `tools/setup-pi-brain.sh` (see above). Boards keep working, they trust the CA.
- **Rotating or replacing the CA means re-provisioning every board**: the CA certificate is stored in each board's NVS from the BLE bundle, so a new CA is only trusted after each board is provisioned again. Treat the CA key as permanent; rotate only if it is compromised.
- Boards verify the certificate dates, so they need a plausible clock (see NTP below).

## Files

| File | Purpose |
|---|---|
| `compose.yml` | Broker service (production, ARM64-capable image) |
| `compose.dev.yml` | Overlay: bind to 127.0.0.1, no auto-restart |
| `mosquitto/mosquitto.conf` | TLS 8883 only, dynsec plugin, anonymous off |
| `mosquitto/new-certs.ps1` | CA + server cert; SANs from parameters |
| `mosquitto/dynsec-bootstrap.ps1` | Initial dynsec config, hub admin, default-deny ACL |
| `mosquitto/node-role.template.json` | Role limiting a node to `home/v1/nodes/<node_id>/#` |
| `mosquitto/dynsec-node-user.ps1` | Create/remove a node user + role (testing; the hub does this in production) |
| `mosquitto/acl-test.ps1` | Isolation + revocation proof against a running broker |

Production: `tools\deploy.ps1 -Target brain -Up`.
ESP32s need the CA cert (delivered in the BLE bundle). Their TLS check needs a valid clock (NTP) and the broker's LAN IP in the SANs.

## NTP (needed for ESP32 TLS)

The ESP32 must know the time before it can verify the broker certificate. The Pi 5 has no battery RTC, so:

1. Install `chrony` on `pi-brain` and use `chrony/chrony-pi-brain.conf` (syncs from `pool.ntp.org`, serves your LAN, keeps serving a free-running clock with `local stratum 10` if the internet is down). Open UDP 123 from the LAN.
2. The BLE provisioning bundle carries `ntp_host` = the Pi's LAN IP (same address as the broker). The firmware tries `ntp_host` first and falls back to `pool.ntp.org`.
3. Open item: with no internet and a Pi that has not synced since boot, the served time can be wrong and TLS date checks may fail. Mitigations to evaluate in the S2a spike: boot-time `makestep`, a fake-hwclock on the Pi, or a long-lived server cert (current server cert: 3 years, CA 10 years).

Status: documented and configured on paper; not tested on hardware.
