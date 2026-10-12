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

All generated material (`certs/`, `data/`, `.env`) is git-ignored. `certs\ca\ca.key` never leaves the PC; `tools\deploy.ps1 brain` copies only `ca.crt`, `server.crt`, `server.key` and `dynamic-security.json`, then on the Pi sets `server.key`, `dynamic-security.json` and `data/` to owner uid 1883 (the container user) with mode 600 via `sudo -n chown/chmod` (needs passwordless sudo for the deploy user). Afterwards the ssh user can only read them with sudo. On the PC, `new-certs.ps1` restricts the key files to your Windows user; Windows cannot set uid 1883 and Docker Desktop's bind mount does not enforce POSIX ownership, so the ownership rule applies on the Pi only.

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
3. Open item: with no internet and a Pi that has not synced since boot, the served time can be wrong and TLS date checks may fail. Mitigations to evaluate in the S2a spike: boot-time `makestep`, a fake-hwclock on the Pi, or a long-lived server cert (current server cert: 825 days, CA 10 years).

Status: documented and configured on paper; not tested on hardware.
