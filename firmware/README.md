# Firmware: ESP32 core (S2a)

Arduino-ESP32 + FreeRTOS node firmware, built with PlatformIO for `esp32dev`. One library
(`esp32-core/`), one short declarative sketch per board (`examples/`). Contract: `contracts/mqtt.md`,
`contracts/announce.schema.json` (schema 2), `contracts/capabilities.md`, `docs/ARCHITECTURE.md` section 4.3.

Configuration goes into NVS through BLE provisioning (S2b, see `BLE-PROVISIONING.md`: PoP, 5-min window, lockout, button),
or through the serial shell / `tools\provision-serial.ps1`. A board needs its per-board PoP (`pop set <code>` over serial, once)
before BLE opens. Nothing here flashes or uploads anything.

## Run steps (Windows 11, PowerShell 5.1)

```powershell
.\firmware\tools\test.ps1        # host unit tests + contract validation of every example (no board needed)
.\firmware\tools\build-all.ps1   # `pio run`: builds all six examples for esp32dev (no upload)
```

Both scripts create/use the repo-local `.venv` (PlatformIO, jsonschema, ziglang). If the PC has no `g++`,
`test.ps1` uses the zig compiler from the `.venv` through `.shim\` wrappers (git-ignored). The first run
downloads the ESP32 toolchain and Arduino core (about 1 GB). To build one example: `.venv\Scripts\pio run -d firmware -e fan`.

"Mock mode" for firmware = the native build: the same core compiled for the PC with a fake HAL
(`FakeHal`), so every driver and safety rule runs without a board.

## Layout

```
firmware/
  platformio.ini            envs: light fan curtain dht relay4 sensors (esp32dev), native (tests)
  esp32-core/               the library (library.json, src/EdgeNode.h = public header)
    src/edge/NodeModel.*    declaration + validation (vocabulary, ranges, pins)
    src/edge/NodeCore.*     pure logic: boot, cmd handling, dedup, safety, announce/state JSON
    src/edge/drivers/       Relay, Pwm, Inputs (PIR, reed, LDR, ADC), Dht, Curtain
    src/edge/EdgeNodeEsp.cpp  ESP32 only: tasks, Wi-Fi, NTP, TLS MQTT, serial shell
    scripts/fw_build_ts.py  writes FW_BUILD_TS (UTC) into a generated header on every build
  examples/                 light, fan (speed), curtain, dht, relay4, sensors (reed + PIR + LDR)
  test/                     Unity tests (native): commands, safety, curtain, misc, examples
  tools/                    test.ps1, build-all.ps1, provision-serial.ps1, check_contracts.py
```

## Declarative API

```cpp
#include <EdgeNode.h>
using namespace edge;
EdgeNode node;

void setup() {
  node.hw("esp32-fan").failsafeAfter(60);
  node.endpoint("ch1", FAN)
      .boolCap("power", RelayOut(26)).bootValue(false).failsafeValue(false).maxOnS(8 * 3600)
      .intCap("speed", 1, 5, PwmOut(27, 25000)).bootValue(1)
      .localToggle("power", 33).localCycle("speed", 32);
  node.endpoint("t1", SENSOR).floatSensor("temperature", DhtSensor(4).temperature(), 30);
  node.begin();   // validates first: an invalid declaration touches no pin and the status LED stays on
}
void loop() { node.loop(); }
```

* Capabilities must be in the fixed vocabulary; kind/type/range are checked against `capabilities.md`
  (e.g. `brightness` 0-100, `speed` needs a range, `lock`/`contact` have fixed enum values, `max_on_s` only on bools).
  Mistakes are listed on the serial console and the node does not start.
* Modifiers (`bootValue`, `failsafeValue`, `maxOnS`, `unit`, `deadband`) apply to the capability declared last.
  Defaults: bool false, numeric = min, enum = first value. `max_on_s`/failsafe are omitted from the announce unless set
  (`max_on_s` is always emitted for bools; 0 = unlimited).
* Sensors take an `interval_s` (the announce field). The state heartbeat is `min(heartbeat, fastest sensor interval)`.
* Local controls: `localToggle` (bool) and `localCycle` (int, wraps max to min) bind a pushbutton to GND (internal pull-up).
  They work with no Wi-Fi and no hub, and their changes are published when the link is back.

## Drivers

| Driver | Use | Notes |
|---|---|---|
| `RelayOut(pin, activeHigh=true)` | `power` on relays/outlets/lights | Inactive level is latched before the pin becomes an output |
| `PwmOut(pin, hz)` / `PwmChannel` | `brightness`, `speed`, with `power` | LEDC. duty = value / max. `PwmChannel(pin,hz).power()` + `.level()` share one MOSFET: duty = power ? level : 0 |
| `PirIn`, `ReedIn`, `SwitchIn` | `motion`, `contact` | Debounced (50 ms), polled every 20 ms, published on change |
| `LdrIn(pin, rFixed, ldrToGnd, luxAt10k, gamma)` | `illuminance` | ADC1 pins only (32-39). Lux conversion is a rough model: **[est]** until calibrated with a lux meter |
| `AdcPercentIn(pin)` | `battery` etc. | ADC1 only |
| `DhtSensor(pin, kDht22).temperature()/humidity()` | DHT11/22 | One physical read (max every 2 s) feeds both capabilities |
| `CurtainMotor(open, close).travelTime(o, c).limitSwitches(o, c).maxRun(ms).deadTime(ms)` | `position` 0-100 (0 closed, 100 open) | See below |

**PWM is for low-voltage DC loads only (DC fans, LED strips through a MOSFET). It cannot dim mains.** Phase-cut mains
dimming needs a zero-cross detector and a triac module and is not implemented. Never connect a mains lamp or fan to a PWM pin.
Fan speed 1..5 maps to 20 %..100 % duty: check that your fan starts at 20 %, otherwise declare a higher minimum.

### Curtain

* One state machine; the open and close outputs are derived from its phase, so both are never on at once. The driver also
  refuses to energise one while the other is on (switches the first off before the second on).
* Reversal always passes through a dead time (default 500 ms) with both outputs off.
* Position from travel-time calibration (stopwatch values), limit switches, or both. With limit switches the travel times
  are learned from end-to-end runs that start with the opposite switch pressed; mid positions are `unsupported` until learned.
  Learned times are kept in RAM only (lost at reboot).
* `maxRun` always applies (default 150 % of the slowest travel). If the limit switch never fires the motor stops and a fault flag is set.
* `boot_value` is the **assumed** position; the curtain never moves at boot. `failsafe_value`, if declared, does move it.
* `position` needs `settle_s` (contract v2): `.settleS(30)` is announced, and validation rejects a value shorter than the driver's
  worst-case motion (longest travel + dead time). The ack means "accepted, motion started" and echoes the accepted target;
  `state` reports the estimated position as it moves and at stop.

## State machine (as built)

```
begin(): validate declaration -> apply boot_value to every actuator -> start tasks
net task:  UNCONFIGURED (no/invalid NVS config: serial shell only, LED 2 Hz)
           -> WIFI -> NTP (until time is plausible; no TLS before) -> MQTT_TLS (CA verified, LWT offline)
           -> on EVERY connect, in this order: status "online" (retained) -> retained announce -> subscribe cmd -> state -> RUN
RUN:       state on change (>= 250 ms apart) + heartbeat; acks after the state they belong to;
           link/Wi-Fi/clock lost -> teardown -> exponential backoff (1 s .. 60 s, +-20 % jitter) -> back to WIFI/NTP/MQTT
actuator task (prio 3, 20 ms): commands, local buttons, driver ticks, max_on_s, failsafe, status LED
sensor task   (prio 1, 20 ms): due sensors are read outside the lock and submitted
```

* **Connection**: clean session; client_id == username == node_id (the node refuses a stored `mqtt_user` that differs; the shell's
  `save` rejects it too). Nothing is queued while offline: the client is destroyed on every teardown, state/acks are only published
  while connected, and acks left over from before a reconnect are dropped.
* **Time rule**: NTP time is accepted only if `FW_BUILD_TS <= t < FW_BUILD_TS + 50 years`
  (`timePlausible`, host-tested). It is re-checked every 10 s in RUN; if the clock goes implausible the node publishes
  `offline`, leaves the TLS session and returns to NTP. Certificate verification and its date checks are never disabled.
  LAN `ntp_host` (if configured) is asked first, then `pool.ntp.org` (`time.cloudflare.com` if no LAN host).
* **Commands**: desired-state only. Whole command validated first against the endpoint's own ranges, nothing applied on any
  error (`unknown_endpoint`, `unknown_capability`, `out_of_range`, `bad_type`, `unsupported`, `bad_request`). Dedup on the last 16
  `cmd_id`s: a duplicate gets the stored ack again and is not applied again. Payloads without a valid uuid `cmd_id` cannot be
  acknowledged and are dropped. `identify` blinks the status LED; `unprovision` acks, publishes `offline`, wipes NVS, reboots.
* **Safety** (all enforced on the node, no hub needed): `boot_value` at boot (before any network), `failsafe_value` once per
  outage after `failsafe_after_s` without the MQTT session (counted from boot if the hub was never reached; 0 = keep last).
  **A physical press wins**: a local press on a capability cancels its pending failsafe until the hub next sends a command that sets
  that capability (then it is armed again); a press after the failsafe was applied just sets the new value.
  `max_on_s` is always enforced: forced off after a continuous on-time whatever turned it on (hub, press, `boot_value`,
  `failsafe_value`); a repeated "on" does not restart it, off then on does.
* "Hub connection" = the MQTT session to the broker (contract: broker-level liveness in v1). A dead hub behind a live broker does not trigger failsafe.
* **Build-time check**: `scripts/fw_build_ts.py` aborts the build if the PC clock is earlier than 2026-10-01T00:00:00Z (1790812800);
  tested by `tools	est_build_floor.py`.
* Tasks never share state without the mutex; acks are never queued across a reconnect; commands are never queued for later.

## Serial configuration (until S2b)

Open the board's USB serial at 115200 baud, or use the helper (it prompts for the two passwords and never logs them):

```powershell
.\firmware\tools\provision-serial.ps1 -Port COM5 -WifiSsid HomeNet -MqttHost 192.168.1.10 `
    -MqttUser esp32-a1b2c3d4e5f6 -CaFile deploy\mosquitto\certs\ca.crt -NtpHost 192.168.1.10
```

Shell commands: `status`, `set <wifi_ssid|wifi_pass|mqtt_host|mqtt_port|mqtt_user|mqtt_pass|ntp_host> <value>`,
`ca clear` / `ca line <pem line>` / `ca end`, `save` (validates, writes NVS namespace `edgecfg`, reboots), `wipe`, `reboot`, `help`.
Replies are `ok` or `err <code>`; secrets are never echoed or logged. Anyone with USB access to the board can reconfigure it:
physical access is the trust boundary, as with the BOOT-button window in the plan. The MQTT user/password come from the hub's
dynsec client (S1) or `deploy/mosquitto/dynsec-node-user.ps1` on the dev PC. The node id is printed at boot (`esp32-<mac>`).

The broker host given here must match the broker certificate (SAN), because the host name is verified.

## What was tested

See the session report: native tests cover everything in `NodeCore`, the drivers (through `FakeHal`), the config shell,
time gate and backoff; the ESP32 glue (`EdgeNodeEsp.cpp`, `HalEsp32.cpp`) is compiled for `esp32dev` but only runs on hardware.

## Hardware test checklist (not done: no board)

1. Boot and serial: banner with node id; `status` shows `incomplete`; LED blinks 2 Hz. Configure with the helper, board reboots.
2. TLS: connects to the TLS broker; record `RUN (heap free ..., min free ...)` from the log. Also with a wrong CA (must fail, never connect)
   and with a wrong host name (must fail).
3. Time: boot with the router's internet blocked and no `ntp_host`: no TLS attempt, LED flashes, retries. Then unblock NTP.
   Set the dev PC clock back, rebuild, flash: a stale build time still works; set a server to a date before the build: must be rejected.
4. Announce on `home/v1/nodes/<id>/announce` (retained) is accepted by device-hub (S1); `state` after every command.
5. Relay board: scope/LED on the pin during power-up (strapping pins 0, 2, 12, 15 can glitch relays); active-low polarity.
6. PWM at 25 kHz: fan speed range, motor start at 20 %; LED strip brightness; no audible whine/flicker.
7. Failsafe on a real disconnect: switch the broker off or pull the AP, wait `failsafe_after_s`, outputs go to `failsafe_value`; reconnect re-arms it.
8. `max_on_s` with a real load. `unprovision` from the hub wipes NVS and the board returns to the 2 Hz LED.
9. Curtain: measure travel times, check interlock with a meter/scope on both outputs while reversing quickly, `maxRun` with the motor disconnected.
10. DHT22 timing under Wi-Fi load (read errors), LDR calibration against a lux meter, reed/PIR behavior.
