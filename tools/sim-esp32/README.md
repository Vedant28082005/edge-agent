# sim-esp32

Python virtual ESP32 nodes that speak the edge-agent contract (`contracts/mqtt.md`, `capabilities.md`, schemas in `contracts/`) like the real firmware: MQTT over TLS, retained announce, status with LWT, state on change + heartbeat, desired-state `cmd` handling with dedup, ack, `identify`, `unprovision`, and the firmware safety behaviour (boot/failsafe/max_on, curtain travel + interlock). It lets device-hub, the agent and the gateway be developed with no hardware. No BLE: credentials come from flags/env (provisioning bypass).

Every outgoing `announce`, `state` and `ack` is validated against `contracts/*.schema.json` before it is published (a violation is counted, logged and **not sent**). The single exception is the deliberate `malformed_announce` fault.

## Setup and tests (Windows 11, PowerShell 5.1)

```powershell
tools\sim-esp32\run-tests.ps1            # creates tools\sim-esp32\.venv on first run (nothing global), runs the unit tests
tools\sim-esp32\run-integration.ps1      # against the dev broker, see below
```

## Run

```powershell
cd tools\sim-esp32
.\.venv\Scripts\python.exe -m simesp32 examples\home3.yaml --check            # validate YAML + announces only
.\.venv\Scripts\python.exe -m simesp32 examples\home3.yaml --mock             # no broker: in-memory bus, messages printed
.\.venv\Scripts\python.exe -m simesp32 examples\home3.yaml `
    --host localhost --port 8883 `
    --ca ..\..\deploy\mosquitto\certs\ca.crt `
    --creds-dir ..\..\deploy\mosquitto\certs\nodes                           # real broker, TLS always verified
```

`--mock` also reads `<node_id> <cmd json>` lines from stdin, e.g. `esp32-a1b2c3d4e501 {"cmd_id":"3f0c1a2b-4d5e-4f60-8a7b-9c0d1e2f3a4b","endpoint":"ch1","set":{"power":true},"by":"agent"}`.

### Credentials (provisioning bypass)

The hub's dynsec client (or the dev helper `deploy\mosquitto\dynsec-node-user.ps1`) creates one MQTT user per node, username = `node_id`, and the node must use `client_id = node_id`. The sim takes the result from:

| What | Flag | Env |
|---|---|---|
| broker | `--host` / `--port` (default `localhost` / `8883`) | `SIM_MQTT_HOST`, `SIM_MQTT_PORT` |
| CA certificate (required, TLS is always verified, TLS 1.2+) | `--ca` | `SIM_MQTT_CA` |
| credentials | `--creds-dir DIR` with `<node_id>.cred` files (`username=` / `password=` lines, the format `dynsec-node-user.ps1` writes) | `SIM_MQTT_CREDS_DIR`; or `SIM_MQTT_PASSWORD_<NODE_ID>` (`-`→`_`, upper-case); single node only: `SIM_MQTT_PASSWORD` |

There is deliberately **no password flag** (argv leaks into process lists). Passwords are never logged or printed.

## YAML

Capabilities use the contract vocabulary; `kind`/`type` come from `capabilities.md`, so you only give the id and its parameters. Names, rooms and aliases are admin labels in the registry, so they are rejected here. The loader enforces the hub's cross-field rules (min ≤ max, boot/failsafe inside range, unique ids, sensors need `interval_s`, actuators need `boot_value`) and fails at start-up, not on the broker.

```yaml
nodes:
  - node_id: esp32-a1b2c3d4e501      # esp32-<12 hex>
    hw: esp32-devkit                  # optional
    fw: 0.1.0-sim                     # optional
    failsafe_after_s: 60              # optional, 0 = keep last values when the hub is lost
    heartbeat_s: 60                   # optional: state republished at least this often
    dedup_size: 16                    # optional: cmd_ids remembered for ack replay (contract: 16)
    seed: 1                           # optional: deterministic generators and fault dice
    faults: {}                        # see below
    endpoints:
      - local_id: ch1
        type: fan                     # light fan outlet lock cover thermostat sensor generic
        sim: {travel_time_s: 12, interlock_s: 0.5, progress_s: 1}   # only for endpoints with `position`
        capabilities:
          - {id: power, boot_value: false, failsafe_value: false, max_on_s: 14400}
          - {id: speed, min: 1, max: 5, boot_value: 1}
```

Sensors: `{id: temperature, interval_s: 30, generator: {...}}`. Generators (`generator.kind`): `constant {value}`, `sine {mean, amplitude, period_s, noise}`, `random_walk {start, step, min, max}`, `sequence {values}` (cycles), `random_choice {values?, change_probability}` (default for bool/enum sensors). All are seeded, clamped to the capability's range, and typed (int/float/bool/enum).

### Example: 3-node home (`examples/home3.yaml`)

| node | endpoints |
|---|---|
| `esp32-a1b2c3d4e501` | `ch1` fan: `power` (max 4 h on), `speed` 1-5 |
| `esp32-a1b2c3d4e502` | `l1` light: `power`, `brightness` |
| `esp32-a1b2c3d4e503` | `c1` curtain: `position` (12 s travel), `t1` temperature + humidity, `m1` motion |

```yaml
nodes:
  - node_id: esp32-a1b2c3d4e501
    endpoints:
      - local_id: ch1
        type: fan
        capabilities:
          - {id: power, boot_value: false, failsafe_value: false, max_on_s: 14400}
          - {id: speed, min: 1, max: 5, boot_value: 1}
  - node_id: esp32-a1b2c3d4e502
    endpoints:
      - local_id: l1
        type: light
        capabilities:
          - {id: power, boot_value: false, failsafe_value: false}
          - {id: brightness, min: 0, max: 100, boot_value: 50}
  - node_id: esp32-a1b2c3d4e503
    endpoints:
      - local_id: c1
        type: cover
        sim: {travel_time_s: 12, interlock_s: 0.5}
        capabilities:
          - {id: position, min: 0, max: 100, boot_value: 0, failsafe_value: 0}
      - local_id: t1
        type: sensor
        capabilities:
          - {id: temperature, interval_s: 30, generator: {kind: sine, mean: 27.0, amplitude: 2.0, period_s: 900, noise: 0.05}}
          - {id: humidity, interval_s: 30, generator: {kind: random_walk, start: 55, step: 0.8, min: 30, max: 80}}
      - local_id: m1
        type: sensor
        capabilities:
          - {id: motion, interval_s: 10, generator: {kind: random_choice, change_probability: 0.15}}
```

(`examples/home3.yaml` is the full file with comments.) Create the three node users first, one per `node_id`, then pass the folder as `--creds-dir`. With the dev helper: `deploy\mosquitto\dynsec-node-user.ps1 -NodeId esp32-a1b2c3d4e501` (and `...02`, `...03`).

## Behaviour

- **Boot:** `boot_value` is applied before any network. **On every successful connect** (boot and reconnect) the node does, in this order: status `online` (retained), the retained `announce` (byte-identical to the previous one), subscribe `cmd`, full `state` (retained). LWT: `status=offline`, QoS 1, retained. Sensors are sampled once at boot so the first state is complete.
- **State:** on every applied command, on each sensor interval, every `heartbeat_s`, after a reconnect, and during curtain travel (`progress_s`).
- **cmd:** atomic (all caps valid or nothing applied). Error codes from `ack.schema.json`: `unknown_endpoint`, `unknown_capability`, `unsupported` (setting a sensor, unknown `cmd`), `out_of_range`, `bad_type` (bool vs int is strict: `1` is not a bool, `true` is not an int, `2.5` is not an int), `bad_request`. Order is apply → publish state → ack. A cmd without a valid `cmd_id` cannot be acked and is dropped.
- **Dedup:** the last **16** `cmd_id`s (count-based ring, `dedup_size` in YAML or `--dedup-size`) keep their ack. A repeated `cmd_id` **replays the ack and does not re-apply or republish state**, so a hub retry after a lost ack is safe.
- **identify:** ack immediately, "LED" on for `duration_s` (log lines). **unprovision:** ack, publish `offline`, disconnect and stay down (the sim does not re-enter BLE mode).
- **failsafe:** after the MQTT link has been down for `failsafe_after_s` (counted from boot if the broker is never reached), every actuator that declares `failsafe_value` is set to it, once per outage; later commands are honoured. `0` keeps the last values.
- **max_on_s:** on bool capabilities. Forces `false` after that much *continuous* on-time (a repeated "on" does not restart it), works offline, state published afterwards.
- **Curtain** (an endpoint with `position`): moves at `100 / travel_time_s` per second. Two relays, `up`/`down`, are never both energised, and a relay is only energised after the opposite one has been off for `interlock_s`. Reversing mid-travel therefore stops, waits, then goes the other way. The announce carries `settle_s` for `position`, derived as `ceil(travel_time_s + interlock_s) + 1` (override with `settle_s:` on the capability; max 120). The **ack is sent when the target is accepted** (values = target); `state.position` is the actual position and follows the travel. `boot_value` for position is the assumed position at boot (no movement).

## Fault injection

In YAML under `faults:` or on the CLI as `--fault [NODE_ID:]KEY=VALUE` (repeatable).

| Fault | Meaning |
|---|---|
| `drop_ack: 0.3` | each ack is silently dropped with this probability (seeded) |
| `drop_first_acks: 2` | drop the first N acks |
| `delay_ack_s: 4` | acks are sent this late (state is published immediately; the hub waits 3 s) |
| `duplicate_cmd: 1` | every incoming cmd is delivered 1 extra time (tests dedup) |
| `duplicate_ack: true` | every ack is published twice |
| `flapping: {up_s: 20, down_s: 5}` (CLI `20:5`) | after `up_s` connected the TCP connection is cut without DISCONNECT (broker fires the LWT), reconnect after `down_s` |
| `malformed_announce: MODE` | `invalid_json`, `missing_field`, `wrong_schema`, `node_id_mismatch` (schema-valid, hub rule), `oversize` (>16 KiB), `unknown_capability` (adds `teleport`, not in the vocabulary). State and acks stay valid. |

`examples/faults.yaml` has one node per fault.

## Tests

`run-tests.ps1`: 126 unit tests using an in-memory transport and a manual clock (no sleeping): contract agreement (vocabulary table vs `announce.schema.json`, every capability announces validly, hub-rule rejections), protocol (boot sequence, retained flags, ordering, dedup, all error codes, atomicity, garbage input, identify, unprovision, heartbeat), safety (boot, failsafe, max_on, curtain travel/interlock incl. "never both relays"), faults, CLI/credential handling.

### Integration against the dev broker

```powershell
# once (deploy\README.md): certs, dynsec bootstrap, broker on localhost:8883
deploy\mosquitto\new-certs.ps1 -LanIp <ip> -TailnetName <name>; deploy\mosquitto\dynsec-bootstrap.ps1
cd deploy; docker compose -f compose.yml -f compose.dev.yml up -d; cd ..
tools\sim-esp32\run-integration.ps1
```

It creates three throwaway node users with `dynsec-node-user.ps1` (dev-PC helper, password on a `docker exec` command line), runs `tests\test_integration.py` and removes the users. Covered: full protocol over real TLS (announce/state validated off the wire, cmd → state → ack, dedup replay, error ack, sensor interval), LWT on an unclean drop (flapping) followed by an identical re-announce, unprovision, wrong/no credentials refused, and the ACL proof with an MQTT v5 probe: a node may publish on `home/v1/nodes/<own id>/…` and subscribe to its own `cmd`, but publishes to another node's `state` and `cmd` and to `perception/…` get reason code `0x87` (Not authorized), a subscription to another node's subtree is denied, and the hub-side observer never sees the denied payloads. Exit code 3 = Docker/broker not available (skipped).

## Limits / assumptions

- No BLE, Wi-Fi or NTP phases; `ts` is the PC clock. The "no valid time, no TLS" rule of `mqtt.md` is not modelled.
- MQTT 3.1.1, clean session, `client_id == username == node_id` (a credentials file with another username is rejected). Nothing is queued while disconnected: publishes are dropped and fresh state goes out on reconnect. A denied publish is only visible to the client under MQTT v5, so the ACL probe in the tests uses v5.
- Hub liveness is judged by the MQTT link only, not by whether device-hub is actually running.
- `max_on_s` is supported on bool capabilities only; config rejects it elsewhere.
