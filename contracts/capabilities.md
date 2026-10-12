# Capability vocabulary (v1, fixed)

Contract version: **1** (paired with announce schema 2). New capabilities or endpoint types are added **only** by a contract change (edit this file, `announce.schema.json`, samples, bump versions). A device can never invent one.

## Capabilities

| Capability | Kind | Type | Range / values | Unit | Notes |
|---|---|---|---|---|---|
| `power` | actuator | bool | | | |
| `brightness` | actuator | int | 0-100 (announce min/max must lie within) | % | |
| `color_temp` | actuator | int | min/max from announce | K | |
| `speed` | actuator | int | **min/max required** in announce | step | |
| `position` | actuator | int | 0-100 | % | covers/blinds |
| `lock` | actuator | enum | `locked`, `unlocked` (fixed) | | |
| `target_temp` | actuator | float | min/max from announce | C | |
| `mode` | actuator | enum | **`values` required** in announce | | values are `[a-z0-9_]{1,24}`, 2-16 items |
| `temperature` | sensor | float | | C | |
| `humidity` | sensor | float | | % | |
| `illuminance` | sensor | float | | lux | |
| `motion` | sensor | bool | | | |
| `contact` | sensor | enum | `open`, `closed` (fixed) | | |
| `power_w` | sensor | float | | W | |
| `current_a` | sensor | float | | A | |
| `voltage_v` | sensor | float | | V | |
| `battery` | sensor | int | 0-100 | % | |

## Endpoint types

`light`, `fan`, `outlet`, `lock`, `cover`, `thermostat`, `sensor`, `generic`.

## Announce rules per capability object

- `id`, `kind`, `type` are required and must match the table (a `power` declared as `sensor` is rejected).
- **Actuators:** `boot_value` required; `failsafe_value`, `max_on_s` optional; `interval_s` not allowed. `boot_value`/`failsafe_value` JSON type follows `type` (bool/int/float/enum string).
- **Sensors:** `interval_s` (1-86400 s) required; `boot_value`, `failsafe_value`, `max_on_s` not allowed.
- `min`/`max` only on `int`/`float`; `values` only on `enum`.
- `unit` optional, from a fixed set: `C % lux W A V K step`.
- `max_on_s` = continuous on-time limit in seconds, 0 = unlimited (firmware-enforced).
- Node-level optional `failsafe_after_s` (default 60, 0 = keep last values).

## Hub-enforced rules (not expressible in JSON Schema)

`min <= max`; `boot_value` and `failsafe_value` inside the range / enum values; `local_id` unique per node; capability `id` unique per endpoint; `node_id` equals the topic id the announce arrived on; `size <= 16 KiB`. Violations are rejected with a logged reason.

## Values on the wire

State and command values use the JSON type of the capability: bool → `true/false`, int → integer, float → number, enum → string. A `set_state` for a `risk: confirm` endpoint additionally needs confirmation (see `mcp-tools.md`).

## UI rendering rule

bool → switch, ranged int/float → slider, enum → select, sensor → read-only readout with unit.
