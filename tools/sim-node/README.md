# sim-node

One simulated ESP32 per process, for the Pi stack on a Windows PC. It is a thin layer over the `tools/sim-esp32` engine (the node side of `contracts/mqtt.md`: connect order, retained announce, LWT, state, 16-entry cmd dedup, acks, failsafe, `max_on_s`, every outgoing message schema-checked) and adds profiles from `firmware/examples`, command latency, `--fail-rate`, and credentials from `.sim/<node_id>/`.

## Prerequisites

- Python 3.10+ on PATH, PowerShell 5.1. Packages go into a local `.venv` (`run-tests.ps1` creates it; nothing global).
- Gateway admin account and a reachable gateway URL; broker at `192.168.29.89:8883` (override with `--host`/`--port`). The broker certificate must cover the name or IP you connect to (TLS is always verified).

## Provision

```powershell
cd tools\sim-node
.\run-tests.ps1                      # first run creates .venv
.\provision-sim.ps1 -NodeId sim-fan-01 -GatewayUrl https://<gateway> -AdminUser admin
```

It prompts for the admin password (SecureString), calls `POST /admin/provision-sessions`, and writes `ca.pem` and `<node_id>.cred` to `.sim\<node_id>\` (git-ignored, owner-only). It prints the node id; **the session expires in about 10 minutes**, so start the sim right away. Use `-WifiProfile <name>` or `-WifiSsid <ssid>` (password prompted) if the gateway wants real Wi-Fi settings; otherwise a placeholder is sent.

**Node ids.** The hub only accepts `esp32-<12 hex>`. `-NodeId`/`--node-id` take either that or a label such as `sim-fan-01`, which maps to `esp32-<first 12 hex of sha256(label)>` (same rule in both tools; the id is printed).

## Run

```powershell
.\.venv\Scripts\python.exe sim_node.py --profile fan --node-id sim-fan-01
.\.venv\Scripts\python.exe sim_node.py --profile dht --node-id sim-dht-01 --latency-ms 50:300 --fail-rate 0.1
```

Run several in separate terminals (each needs its own provisioning). Options: `--host`, `--port`, `--sim-dir`, `--ca`, `--latency-ms MIN:MAX` (default `50:300`), `--fail-rate 0..1` (set-commands refused with ack error `busy`, not applied), `--heartbeat-s`, `--seed`, `--check` (print announce), `--mock` (in-memory bus, no broker; stdin lines are cmd JSON). Ctrl-C publishes `offline`; a killed process fires the LWT. One log line per command: `cmd 3f0c1a2b ch1 set={"power":true} by=agent -> ok (+93 ms)`.

| profile | endpoints (from firmware/examples) |
|---|---|
| `light` | `l1` light: `power`, `brightness` |
| `fan` | `ch1` fan: `power` (8 h max on), `speed` 1-5 |
| `curtain` | `c1` cover: `position` (20 s travel, `settle_s` 30) |
| `dht` | `t1`: `temperature`, `humidity` every 30 s, slow random walk |
| `relay4` | `r1`..`r4`: `power` (r1 1 h limit, r3 boots on) |
| `sensors` | `door` contact, `hall` motion, `lux` illuminance |
| `room` | one node, 9 endpoints: `l1`,`l2` lights (`power`, `brightness`), `ch1` fan (`power`, `speed`), `c1` curtain (`position`), `t1` (`temperature`, `humidity`), `r1`..`r4` relays (`power`) |

The announce limit is 16 endpoints per node and the contract has no per-type restriction, so `room` fits in a single node and a single approval.

The announce has no name field (names/rooms are admin labels, and the schema forbids extra keys), so the sim is marked in `hw` (`sim-fan`, ...) and `fw` (`sim-0.1.0`).

## A whole room at once

```powershell
.\start-room.ps1 -GatewayUrl https://<gateway> -AdminUser admin                      # 4 nodes: light, fan, curtain, dht
.\start-room.ps1 -GatewayUrl https://<gateway> -Count 3 -Profiles room,relay4,sensors
.\stop-room.ps1
```

`-Count` nodes cycle through `-Profiles` and are labelled `sim-<profile>-NN` (`sim-light-01`, `sim-fan-01`, ...). The admin password is asked once at a hidden prompt and kept in memory only. Each sim runs as a hidden background process; PIDs are in `.simoom.pids.json`, logs in `.sim\<node_id>\sim.log`. The script prints the node ids; approve each in the dashboard. `stop-room.ps1` stops only the recorded PIDs (after checking each is still that sim); the broker then fires the LWT and the nodes show offline.

## Approve in the dashboard

Admin > pending nodes: the sim appears once it announces. Select it, give each endpoint a name and room, approve. Then control it from the dashboard or chat.

## Tests

```powershell
.\run-tests.ps1                      # 48 unit tests, in-memory broker, manual clock
```

## Cleanup

Stop the sim (Ctrl-C), forget the node in the dashboard (this deletes its MQTT user and retained topics), then `Remove-Item -Recurse .sim\<node_id>`.
