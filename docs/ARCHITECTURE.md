# Edge Agentic IoT Framework: Architecture and Build Plan (v2)

Minor project: LLM-based multimodal agentic AI framework for intelligent IoT systems (PDEU).

Scenario: smart home. Everything runs on edge hardware; dev on PC, validate on ARM64.

Labels: [measured] = measured on the real hardware. [est] = estimate, must be measured. [decision] = decided with the owner.

---

## 0. Decisions log

| # | Decision |
|---|---|
| D1 | Brain = base Qwen2.5-7B Q4 with tool calling on the 8GB Pi. The HomeBench fine-tune is optional, off the critical path. |
| D2 | All servers on Tailscale (MagicDNS names, static tailnet IPs). ESP32s on the LAN, reaching the broker at the brain Pi's static LAN address. |
| D3 | Node/endpoint device model. One physical ESP32 (node) exposes one or more logical devices (endpoints). |
| D4 | Fixed message schemas, a fixed capability vocabulary, one fixed firmware state machine. Firmware option A: core library plus a short declarative sketch per board. A universal template firmware (option B) is a stretch goal. |
| D5 | Admin-only device management (add, remove, rename, rebind, re-provision) from the Android app or the web dashboard, via `/admin/*` REST. The LLM has no access to any of it. |
| D6 | BLE provisioning, started by an admin. Provisioning window opens by button press (or on first boot) and lasts 5 minutes. Per-board PoP is reserved for later. |
| D7 | MQTT over TLS from the start, per-device MQTT credentials and ACLs via the Mosquitto dynamic-security plugin. |
| D8 | OTA is not in scope; the protocol keeps a reserved hook. |
| D9 | No iOS. Android via Flutter, web via Chromium (Web Bluetooth). |
| D10 | Scenes in v1; automations in the agentic phase. Scenes reference endpoints by stable id or by selector. Offline endpoints are skipped and reported, never queued. Members may create scenes for rooms they can access. |
| D11 | Multi-user with roles `admin` / `member` / `guest`. |
| D12 | Dashboard: React + Vite + TypeScript + Tailwind. |
| D13 | Agentic features are built as hooks in v1 and filled in if time permits (section 9). |

### Changes from v1 of the plan

Device model is now node/endpoint. MQTT topics are per node. Added BLE provisioning, TLS, per-device credentials, safety fields in the announce, scenes with selectors, and the agentic hooks. The claim-code and "pending approval" flow is replaced by admin-initiated provisioning.

---

## 1. Principles

1. **Modular by contract.** Services talk only through the contracts in section 4. No service imports another's code.
2. **Devices are plug-and-play.** A provisioned ESP32 announces itself; the registry, dashboards, prompt and constrained decoding all rebuild from the registry. No code change to add or remove a device.
3. **Perception is optional.** No camera anywhere must still be a fully working system.
4. **Cheapest sufficient path first.** The scheduler routes by measured cost.
5. **Never execute model output blindly.** Every action is validated against the live registry (endpoint, capability, range, risk) before it reaches hardware.
6. **The LLM acts, the admin governs.** Runtime control tools are separate from management endpoints. Device-supplied text is untrusted data.
7. **Raw frames and audio stay local.** Only text/JSON crosses between nodes, except uploads to the node that processes them. Temp media is deleted after use.
8. **Everything has a mock mode** so each component builds and tests on the PC.
9. **Graceful degradation.** Jetson down: still works. Vision Pi down: text and device control still work. LLM busy: fast path still works.
10. **Safety lives in the firmware too.** The hub is not the only safety layer.

---

## 2. Topology

| Node | Tailnet name | Runs |
|---|---|---|
| Pi 5 8GB | `pi-brain` | llama-server (Qwen2.5-7B Q4), **agent**, **device-hub** (registry, MCP server, MQTT bridge, dynsec client), **gateway** (auth, REST/WS, admin API, serves React build), mosquitto (TLS), SQLite |
| Pi 5 4GB | `pi-vision` | **vision-node**: Qwen3-VL-2B (llama-server) + whisper.cpp server, one shared priority queue |
| Jetson Nano 4GB (JetPack 4.6) | `jetson-yolo` | **yolo-node**: YOLOv8n TensorRT FP16, optional camera, frame-upload endpoint |
| ESP32s | LAN only | lights, fans, sensors, relays, etc. |
| Clients | tailnet | React dashboard (Chromium), Flutter Android app |

Ports (proposal): mosquitto 8883 (TLS; 1883 disabled), llama-server 8080 (localhost), gateway 8000 (HTTPS via `tailscale serve`), agent 8200 (localhost), device-hub 8100 (localhost), vision-node 8300, yolo-node 8400.

Process management: llama-server and the VLM/whisper servers native via systemd. Other services in docker compose (ARM64 images built with buildx).

Whisper runs on a different Pi from the 7B, so STT and LLM can overlap. Whisper and the VLM share the 4GB Pi and are serialized there (voice has queue priority).

---

## 3. Components at a glance

```
 React dashboard ─┐                                   ┌─ ESP32 nodes (MQTT/TLS)
 Flutter app ─────┤→ gateway ⇄ agent ⇄ device-hub(MCP)┤
   (admin + user) │   (auth,    │  │       │          └─ registry (SQLite)
                  │   /admin/*) │  │       └─ mosquitto (TLS + dynsec)
                  │             │  └─ llama-server (7B)
                  │             ├──→ vision-node (pi-vision): /transcribe, /describe
                  │             └──→ yolo-node (jetson): /detect, events over MQTT
                  └─ WS: live state, events, chat stream
```

---

## 4. Contracts (build these first)

### 4.1 Device model

- **Node** = a physical ESP32. Identity = `node_id` (derived from the MAC, e.g. `esp32-a1b2c3d4e5f6`). Carries firmware version, health, credentials.
- **Endpoint** = a logical device the user controls ("bedroom fan"). Belongs to one node, has a `local_id` (e.g. `ch1`), a type and capabilities.
- When an admin approves an endpoint, the hub assigns a permanent `endpoint_id`. **Name, room and aliases are editable labels; the id never changes.** The admin can rebind an endpoint to a different node (hardware swap) and the id stays.
- Cameras are not endpoints. They are **image sources** (section 4.9).

**Standard capability vocabulary (fixed, versioned)**

| Capability | Kind | Type / range | Unit |
|---|---|---|---|
| `power` | actuator | bool | |
| `brightness` | actuator | int 0-100 | % |
| `color_temp` | actuator | int | K |
| `speed` | actuator | int, min/max from announce | step |
| `position` | actuator | int 0-100 | % (motion capability: the announce must carry `settle_s`, 1..120 s) |
| `lock` | actuator | enum `locked`/`unlocked` | |
| `target_temp` | actuator | float | °C |
| `mode` | actuator | enum (values from announce) | |
| `temperature` | sensor | float | °C |
| `humidity` | sensor | float | % |
| `illuminance` | sensor | float | lux |
| `motion` | sensor | bool | |
| `contact` | sensor | enum `open`/`closed` | |
| `power_w`, `current_a`, `voltage_v` | sensor | float | W / A / V |
| `battery` | sensor | int | % |

Endpoint types: `light`, `fan`, `outlet`, `lock`, `cover`, `thermostat`, `sensor`, `generic`. New capabilities and types are added to the vocabulary by a contract change, never ad hoc by a device. The dashboard and Flutter app render controls from the schema (bool becomes a switch, ranged int a slider, enum a select, sensor a readout), so no UI code per device.

### 4.2 MQTT (broker on pi-brain, TLS on 8883)

Prefix `home/v1/`. Payloads JSON, UTF-8. MQTT `client_id` == MQTT `username` == `node_id`, clean session (cmd is never retained, so no queued old commands). Each node has its own MQTT user, created at provisioning time through Mosquitto's dynamic-security plugin, with a role that allows publish/subscribe only on `home/v1/nodes/<its node_id>/#`. User creation by services is idempotent: if the client exists, set its password and ensure its role. The hub has a privileged account. Anonymous access is off.

| Topic | Dir | QoS / retain | Purpose |
|---|---|---|---|
| `nodes/{id}/announce` | node→hub | 1, retained | Self-description, republished on every boot |
| `nodes/{id}/status` | node→hub | 1, retained | `online` / `offline` (offline set by LWT) |
| `nodes/{id}/state` | node→hub | 1, retained | Values for every endpoint capability |
| `nodes/{id}/cmd` | hub→node | 1 | Command |
| `nodes/{id}/ack` | node→hub | 1 | Command result |
| `nodes/{id}/ota` | reserved | | Not implemented (D8) |
| `perception/{node}/events` | node→agent | 1 | Detection/change events |
| `services/{name}/health` | service→hub | 0, retained | CPU, MemAvailable, temp, queue depth |

**announce (schema 2)**

```json
{
  "schema": 2,
  "node_id": "esp32-a1b2c3d4e5f6",
  "hw": "esp32-devkit",
  "fw": "0.1.0",
  "endpoints": [
    {
      "local_id": "ch1",
      "type": "fan",
      "capabilities": [
        {"id": "power", "kind": "actuator", "type": "bool",
         "boot_value": false, "failsafe_value": false, "max_on_s": 0},
        {"id": "speed", "kind": "actuator", "type": "int", "min": 1, "max": 5,
         "boot_value": 1}
      ]
    },
    {
      "local_id": "t1",
      "type": "sensor",
      "capabilities": [
        {"id": "temperature", "kind": "sensor", "type": "float", "unit": "C", "interval_s": 30}
      ]
    }
  ]
}
```

- Capability ids must be in the standard vocabulary. The hub validates every announce against the JSON Schema and rejects bad ones with a logged reason.
- Names, rooms and aliases are **not** part of the announce. They are admin-set in the registry. Any string a device sends is length-limited and sanitized, and never placed raw in the LLM prompt.
- `boot_value` is applied at boot, `failsafe_value` after the hub connection is lost for `failsafe_after_s` (default 60 s; 0 = keep last), `max_on_s` forces off after a continuous on-time (0 = unlimited). These are enforced by the firmware.

**state**: `{"ts": 1760000000, "endpoints": {"ch1": {"power": true, "speed": 3}, "t1": {"temperature": 27.4}}}`

**cmd**: `{"cmd_id": "uuid", "endpoint": "ch1", "set": {"power": true, "speed": 3}, "by": "user:vedant"}`

Commands are **desired-state, never toggles**, so retries are safe. Nodes keep the last 16 `cmd_id`s (count-based, no TTL, not persisted across reboot) and replay the cached ack for a repeat; the hub never reuses a `cmd_id` (UUIDv4). Nodes also accept `{"cmd_id": ..., "cmd": "identify", "duration_s": 10}` and `{"cmd_id": ..., "cmd": "unprovision"}` (wipe stored config, return to provisioning mode).

**ack**: `{"cmd_id": "uuid", "ok": true, "endpoint": "ch1", "values": {...}, "error": null}`. The ack means "accepted, motion started"; `values` echo the accepted target and `state` reports actual progress. Capabilities that announce `settle_s` (required for `position`, 1..120 s) are verified by the hub: `set_state` returns `moving`, then state must converge (+/-2 for position) within `settle_s`, else an `action_settle_timeout` event. Capabilities without `settle_s` return `done`. The ack timeout stays 3 s. Nodes do not queue publishes while disconnected; an ack lost in an outage is covered by the ack timeout.

**perception event**: `{"ts": ..., "node": "jetson-yolo", "source_id": "cam0", "kind": "detection_change", "detections": [{"label": "person", "conf": 0.81, "bbox": [x,y,w,h]}], "summary": "person appeared"}`

### 4.3 Firmware flow (identical on every board)

```
BOOT → LOAD_CONFIG
  ├─ no config, or BOOT button held 3 s → PROVISIONING (BLE, 5 min window, LED pattern)
  │     ├─ success → save to NVS → reboot
  │     └─ timeout → idle (no BLE) until the button is pressed or power-cycled
  └─ config present → apply boot_value → WIFI → NTP (valid only if ≥ firmware build timestamp; retry, no TLS before) → MQTT_TLS (client_id = username = node_id, clean session, LWT=offline)
        → status online → ANNOUNCE (retained) → subscribe cmd → publish state → RUN
        (this exact sequence repeats on every reconnect, not only at boot; an identical announce is a no-op for the hub)
RUN: handle cmd (dedup cmd_id, validate vs own ranges, apply, publish state, ack)
     publish state on change + heartbeat; sensors on interval
     link lost → reconnect with backoff in its own task; after failsafe_after_s apply failsafe_value
     local buttons keep working offline
```

- Core library (option A, [decision]): one declarative call per capability (e.g. `addBool("power", setRelay)`, `addInt("speed", 1, 5, setSpeed)`, `addSensorFloat("temperature", 30, readDht)`). The library does Wi-Fi, NTP, TLS, MQTT, announce JSON, acks, dedup, safety timers, BLE provisioning, `identify`, `unprovision`.
- FreeRTOS tasks: network/MQTT, provisioning, sensor sampling, actuator control. Pin-level code stays in the user's callbacks.
- TLS needs a valid clock, so NTP sync happens before the MQTT connect. Time earlier than the firmware build timestamp is never accepted: the board keeps retrying NTP (LAN chrony on `pi-brain` first, `pool.ntp.org` fallback) and does not connect over TLS until the time is plausible. The Pi 5 keeps time across power cuts with an RTC backup battery (`deploy/README.md`). Open item: verify behavior when there is no internet (section 11).

### 4.4 BLE provisioning (admin-initiated)

1. Admin taps **Add device** (Flutter app, or web dashboard in a Chromium browser over HTTPS). The gateway checks the `admin` role.
2. The board is in its provisioning window (first boot, or BOOT button held 3 s; window = 5 min, then BLE turns off).
3. The client scans for the provisioning service UUID and lists boards (`Setup-XXXX`). The admin picks one; the board blinks its LED (identify).
4. The client reads the board's `node_id` and info, then calls `POST /admin/provision-sessions {node_id}`. The gateway creates the board's MQTT user and role through dynsec and returns a **bundle**: Wi-Fi SSID and password (entered by the admin or taken from a saved profile), broker host and port, the board's MQTT username and password, the CA certificate. The bundle is sent to the board over the BLE data characteristic (chunked, encrypted session).
5. The board stores everything in NVS, turns BLE off, joins Wi-Fi, connects with TLS and announces.
6. The hub matches the announce to the pending session; the admin names each endpoint (room, name, aliases, risk).
7. If the board does not connect within about 10 minutes, the gateway deletes the pending record and the MQTT user.

BLE GATT spec lives in `contracts/ble-provisioning.md` and is implemented once in firmware, once in Flutter (`flutter_blue_plus`), once in the web client (Web Bluetooth).

Notes and limits:

- Trust in v1 is only the physical button window. BLE "just works" pairing gives no protection against a man in the middle during that window. The per-board PoP (printed secret, QR) is **reserved in the protocol (field exists, unused)** and added later.
- Web Bluetooth works only in Chromium browsers over HTTPS (use `tailscale serve`). Flutter on Android is the fallback.
- Espressif's provisioning library handles encrypted BLE sessions but is Wi-Fi-focused. Sending broker credentials and a CA certificate needs a custom data endpoint. Verify in the first spike whether the Arduino-ESP32 wrapper exposes this; otherwise write a small custom GATT service with an encrypted payload.

### 4.5 Registry (SQLite WAL, owned by device-hub)

Tables: `nodes`, `endpoints` (endpoint_id, node_id, local_id, type, name, room, aliases, tags, risk, enabled), `capabilities`, `rooms`, `scenes`, `scene_actions`, `users`, `user_rooms`, `sources`, `provision_sessions`, `audit_log`. `tags` are admin-set labels (max 8 per endpoint, each 1..24 chars of `[a-z0-9_-]`), edited with `PATCH /admin/endpoints/{id}`; they are **not part of the announce** and a device cannot set them. A registry change emits an event so the agent rebuilds (a) the prompt's device list and (b) the constrained-decoding schema.

### 4.6 Management (admin) vs runtime control

**Admin REST (gateway `/admin/*`, role `admin` only; both clients use the same endpoints)**

`POST /admin/provision-sessions`, `GET /admin/nodes`, `POST /admin/nodes/{id}/approve` (name and place each endpoint), `PATCH /admin/endpoints/{id}` (rename, room, aliases, tags, risk, enable/disable), `POST /admin/endpoints/{id}/rebind`, `POST /admin/nodes/{id}/identify`, `POST /admin/nodes/{id}/unprovision`, `DELETE /admin/nodes/{id}` (forget).

- **Forget:** the hub shows what uses it ("2 scenes, 1 automation"). On confirmation it deletes the board's MQTT user and role (**real revocation**), clears retained topics, soft-deletes registry rows, and marks dependent scenes/automations as broken (not silently deleted).
- Every admin action is audit-logged.

**MCP server (device-hub): runtime tools only, a small fixed set.** The tool list never changes as devices come and go.

| Tool | Args | Notes |
|---|---|---|
| `list_devices` | `room?`, `type?` | Endpoints with room, type, online status |
| `get_state` | `endpoint_id` or `room` | Current values |
| `set_state` | `endpoint_id`, `capability`, `value` | Validated against the registry; waits for ack (3 s timeout, unchanged); returns status `done`, or `moving` for capabilities with `settle_s`, after which the hub verifies convergence within `settle_s` (exact for bool/enum, +/-2 for int percentages) and emits `action_settled` or `action_settle_timeout`; `risk: confirm` endpoints need a `confirmation_token` (see below) |
| `run_scene` | `scene` | See 4.7. Per-action results use the same statuses (`done`, `moving`, skipped, failed). Same `confirmation_token` rule; if any action needs confirmation and has no valid token, **nothing runs** |
| `read_sensor` | `endpoint_id`, `capability` | Last value, timestamp, staleness flag |

The LLM can **propose** a scene or automation as JSON, but it is saved only after a user approves it in the UI.

**Confirmation tokens.** `risk: confirm` actions need a `confirmation_token`: opaque, single-use, bound to the user and the exact action, 60 s TTL, minted by the gateway (`POST /confirmations`) only when the user approves in the UI. It is **not** in any tool schema the LLM sees; the agent executor attaches it out-of-band (MCP `_meta`), and device-hub validates and consumes it through the gateway. Without a valid token the tool returns `confirmation_required` / `needs_confirmation` and the agent emits a `confirmation_request` event. A decline or timeout produces `confirmation_declined` / `confirmation_expired`; there is **no automatic re-prompt**, the UI offers a **Retry** that creates a brand-new request. Proactive and automation-triggered actions never prompt: for `risk: confirm` endpoints they only notify. Details in `contracts/mcp-tools.md` and `contracts/agent-events.md`.

### 4.7 Scenes (v1)

- A scene is a list of actions `{target, capability, value}`. `target` is either a fixed `endpoint_id` or a **selector** `{room?, type?, tag?}` resolved at run time (`tag` matches the endpoint's admin-set `tags`, section 4.5; fields combine with AND), so a new light added to the bedroom automatically joins "good night".
- Run semantics: validate each action, run through the same executor as everything else (range checks, `risk`, verify). **Endpoints that are offline, or lack the capability, are skipped and reported** ("fan offline, skipped"); commands are never queued, so a stale "unlock" cannot fire later. Results are per action (partial failure is visible).
- `risk: confirm` endpoints in a scene need a `confirmation_token` from the user running it. If any action targets such an endpoint without a valid token, **the scene executes nothing** (not even its non-risky actions) and returns `needs_confirmation` listing those endpoints; the token is bound to the scene and to the exact risky actions it resolved to.
- Permissions: admins create, edit and delete all scenes. Members create and edit their own scenes for rooms they can access. Guests can run assigned scenes only. Automations (later) never execute actions on `risk: confirm` endpoints, with **no admin opt-in**, and never prompt: they notify instead, and the user can then act through the normal confirmation flow. A future **standing approval** (a user pre-approving a specific `risk: confirm` action for an automation, with limits and expiry) is an idea explicitly **out of scope** for this project.
- The registry re-validates all scenes and automations on every registry change and flags the broken ones.

### 4.8 Perception APIs

**vision-node (pi-vision :8300)**

- `POST /transcribe` (audio file; wav/webm/m4a/aac, converted to 16 kHz server-side) → `{text, duration_ms, filtered}`. Post-filters junk (`you`, `[BLANK_AUDIO]`, music notes).
- `POST /describe` multipart `{image, mode: yesno|short|custom, question, schema?}` → `{answer|json, latency_ms, mode, cached}`. Constrained decoding on `short` and `custom`. Cache (image hash, question), 30 s TTL.
- `GET /health` → `{mem_available_mb, queue_depth, busy, temp_c, models}`.
- One heavy job at a time, bounded queue (429 on overflow), `transcribe` has priority over `describe`, reject if MemAvailable < 400 MB, 25 s timeout → `{"error": "vision_unavailable"}`.

**yolo-node (jetson :8400)**

- `POST /detect` multipart `{image}` or `{source_id}` → `{detections, latency_ms}`. Works with no camera (uploaded or test images).
- `GET /sources`, `POST /sources`, `GET /health`.
- A camera source, if enabled, runs continuously and publishes events only on change (debounced, thresholded).

### 4.9 Image sources (camera optional)

A source is `{source_id, type, location, node, online}`. Types: `upload` (always available), `usb`, `csi`, `rtsp`/`http_snapshot` (e.g. ESP32-CAM, phone), `folder_replay` (test images for evaluation and demos). Sources register in the registry. The agent exposes `describe_scene(question, source_id?)` and `detect_objects(source_id?)` only if at least one source is online or an image is attached to the current turn; otherwise it answers "no camera available". The camera can live on the Jetson or the vision Pi.

### 4.10 Agent API (internal)

`POST /v1/chat` `{user_id, session_id, text?, audio_ref?, image_refs?}` → SSE stream: `status` ("checking the camera"), `tool_call`, `tool_result`, `token`, `final`, `async_result` (late vision answer). The agent never blocks on vision.

### 4.11 Gateway API (user-facing)

REST: `/auth/login`, `/auth/refresh`, `/users` (admin), `/devices` (list; read-only for non-admins), `/devices/{endpoint_id}/set`, `/scenes` (CRUD per permissions) and `/scenes/{id}/run`, `/chat` (SSE), `/voice`, `/images`, `/memory` (the caller's own), `/system/health`, plus `/admin/*` (4.6). WebSocket `/ws`: live state, perception events, notifications, async results.

Auth: JWT (access + refresh), argon2 password hashes, roles `admin` | `member` | `guest` (guest: assigned scenes and limited rooms, no `risk: confirm`). Per-user conversations and memory. Tailnet-only exposure plus Tailscale ACLs. Audit log for admin and device actions.

---

## 5. Scheduler / router (algorithmic contribution)

| Need | Route | Expected cost |
|---|---|---|
| Plain device command or scene ("turn off bedroom light", "good night") | **Fast path**: rule/grammar intent parser + fuzzy match (e.g. rapidfuzz) on aliases/rooms/types → validated `set_state` / `run_scene` | milliseconds [est] |
| Person/object presence | YOLO | ~94 ms on Pi [measured]; Jetson not yet measured |
| Numeric state | `read_sensor` | ms |
| Yes/no visual question | VLM `yesno` | ~2.4 s [measured] |
| Open visual question | VLM `short` | ~5.5 s [measured] |
| Structured scene | VLM bounded schema | 12-15 s [measured], only if explicitly needed |
| Ambiguous, multi-step, conversational, planning | **LLM tool-calling loop** | 15-30 s per turn [est from measured 9 tok/s prefill, 2 tok/s generation], cold prefix minutes |

- Prompt prefix is static and cached: system prompt + compact tool schemas + device list (names, rooms, types only, **no live state**; fetch state with `get_state`). Rebuilt only when the registry changes. History goes in the tail (multi-user history differs every turn).
- The fast path must absorb most smart-home commands; this is the headline measurable result. Fallback option if LLM latency is unacceptable: a smaller model (e.g. Qwen2.5-3B) for tool calling, decided from measurements.
- Cost table updated online (EWMA latency per route); node health and `MemAvailable` gate routes (VLM rejected below 400 MB; Jetson down → YOLO skipped or run on the Pi).
- Fast path executes only with a unique, high-confidence match; ties escalate to the LLM or trigger a clarifying question.
- Event-triggered escalation (YOLO change → optional VLM check → notify); no timer polling of the VLM.
- **Constrained decoding:** the LLM's action output is constrained by a JSON schema/grammar **generated from the live registry** (enum of endpoint ids, capabilities, ranges). Invalid endpoints become impossible. Report ablation: unconstrained vs constrained on valid and invalid instructions (relates to the ~5% invalid-executed rate measured on the HomeBench fine-tune).

---

## 6. Memory and context (agent)

| Layer | Holds | Where | In prompt? |
|---|---|---|---|
| Static prefix | System prompt, compact tool schemas, device list | Built from registry | Yes, cached; rebuilt on registry change |
| Short-term | Last 1-2 turns plus rolling summary, per user/session | SQLite | Yes, in the tail |
| Long-term user memory | Preferences and facts | SQLite `memories`, per user | Top-k only, ≤150 tokens, tail |
| Household facts | Shared facts, admin-editable | SQLite `home_facts` | Top-k with user memories |
| Live state | Device/sensor values | Registry/MQTT is the source of truth | **No**, fetched with `get_state` |
| Action log | Who, what, when, why, result | Audit log | No |

- Retrieval: SQLite FTS5 keyword search plus recency and tags; no embedding model on the busy Pi (revisit later).
- Writes happen off the critical path: explicit statements ("remember...", "I prefer...") by simple rules, everything else extracted by the LLM only when idle, batched and cancelled the moment a user request arrives. Summaries the same way.
- Per-user memories are never shared; only household facts are, edited by admins. Guests have no persistent memory. Users can view, edit and delete their memories.
- Prompt budget [est], enforced in code: system ≈ 300, tool schemas ≈ 300, device list ≈ 200-400, memories ≤ 150, recent turns ≤ 400, current ≤ 100 tokens. If the device list outgrows its budget, send only the rooms mentioned.
- 8GB Pi RAM [est]: 7B Q4 ≈ 4.5 GB, KV cache at 4k context ≈ 0.25 GB, OS ≈ 0.5 GB, services ≈ 0.3-0.5 GB, so about 5.6-6 GB total. Measure `MemAvailable` and keep ≥ 500 MB.

---

## 7. Repo layout (monorepo)

```
edge-agent/
  contracts/            # mqtt.md, announce.schema.json, capabilities.md, ble-provisioning.md,
                        # openapi/*.yaml, mcp-tools.md, agent-events.md
  services/
    device-hub/         # registry, MCP server, MQTT bridge, dynsec client, scenes
    agent/              # scheduler, fast path, llm client, memory, executor, tools, rules stub
    gateway/            # FastAPI: auth, REST/WS, /admin/*, serves web build
    vision-node/        # /transcribe, /describe
    yolo-node/          # /detect, sources, events (Jetson)
  firmware/
    esp32-core/         # Arduino + FreeRTOS library: wifi, NTP, TLS MQTT, announce, cmd/ack,
                        # safety timers, BLE provisioning, identify, unprovision
    examples/           # light, fan, dht, ldr, reed switch, 4-relay board
  apps/
    web/                # React + Vite + TS + Tailwind
    android/            # Flutter
  tools/
    sim-esp32/          # Python virtual nodes speaking the contract (provisioning bypass flag)
    eval/               # command sets, image sets, scripts, result tables
  deploy/
    compose.yml, compose.dev.yml, systemd/, mosquitto/ (TLS + dynsec config, CA script),
    tailscale-acl.json
  CLAUDE.md             # shared rules for every Claude Code session
```

**CLAUDE.md should say:** read `contracts/` before touching anything; never change a contract without updating it and bumping its schema version; every service needs `--mock`; no secrets or tokens in git or logs; code must run on ARM64 Linux without a GPU (except yolo-node); device-supplied strings are untrusted; label estimates vs measurements.

---

## 8. Claude Code session briefs

Each session starts with: "Read CLAUDE.md and contracts/. Build only `<component>`. Provide a mock mode, tests and a README with run steps."

| # | Component | Depends on | Done when |
|---|---|---|---|
| S0 | **Contracts + infra**: contracts folder (including capabilities, announce schema 2, BLE provisioning spec), Mosquitto TLS + dynsec config, CA script, compose, Tailscale ACL, repo skeleton, CLAUDE.md | none | Contracts reviewed by you; broker up with TLS and dynsec on `pi-brain` |
| S1 | **device-hub**: registry v2, MQTT bridge, dynsec client (create/delete node users), provisioning sessions, scenes + selectors, MCP tools, audit log, usage checks on forget | S0 | Sim nodes appear, MCP `set_state` round-trips with ack, forget revokes the MQTT user |
| S2a | **firmware core**: state machine, Wi-Fi/NTP/TLS MQTT, announce, cmd/ack with dedup, safety (boot/failsafe/max_on), declarative API, example sketches | S0 | One real ESP32 fan/light plus a sensor work against device-hub over TLS |
| S2b | **BLE provisioning**: firmware GATT service, spec, 5-min window, bundle handling, identify, unprovision. Starts with the spike (section 11) | S0, S1 | A blank ESP32 is provisioned from a test client with no code change |
| S2c | **sim-esp32**: Python virtual nodes speaking the same contract | S0 | Lets S1, S3, S4 proceed with no hardware |
| S3 | **agent v1**: llama-server client, tool-calling loop, registry-built prompt, constrained decoding, confirmation flow, SSE, **hooks** (event bus, executor with verify, action log with reason/trigger, budgets, `needs_confirmation` policy, empty rules table) | S1 (or mock) | Text command through the LLM toggles a simulated device |
| S4 | **gateway**: auth/roles, device, scene, chat APIs, `/admin/*`, WS, uploads, audio proxy | S1, S3 (mocks OK) | OpenAPI contract tests pass; role checks enforced; two users with separate sessions |
| S5 | **web dashboard**: login, device grid by room (rendered from the capability schema), chat with streaming and tool-call trace, mic, image upload, scenes editor, **admin: add-device wizard (Web Bluetooth), approve/name endpoints, rename, rebind, forget**, system-health and scheduler view, memory page | S4 | Full control, chat and device management from a Chromium browser over the tailnet |
| S6 | **vision-node**: whisper + VLM wrappers, priority queue, memory guard, caching, constrained schemas, health | S0 | `/transcribe` and `/describe` meet the measured-behavior checks |
| S7 | **yolo-node**: TensorRT FP16 engine (export ONNX on the PC with a TRT 8.2-compatible opset, build the engine with `trtexec` on the Jetson), `/detect`, sources abstraction, change-event publisher | S0 | `/detect` works with no camera; events publish with a camera; latency/RAM/power measured |
| S8 | **scheduler + fast path + memory** (inside `agent`): intent parser, fuzzy matcher, cost table, routing, async vision, event escalation, memory extraction; then agentic extras (section 9) | S3, S6, S7 | Routing decisions visible in the dashboard; ablations runnable |
| S9 | **Flutter app**: login, device control, chat, push-to-talk, image upload, scenes, **admin add-device wizard (BLE via `flutter_blue_plus`)**, live state, notifications | S4, S2b | Same features as web, plus reliable BLE provisioning on Android |
| S10 | **eval harness + report tables** | all | One command produces the tables in section 10 |

Parallelism: after S0 run S1, S2a, S2c, S6, S7 in parallel; S2b starts with its spike right away. S3 and S4 start against mocks. S5 and S9 start against the OpenAPI spec with a mock gateway.

---

## 9. Agentic roadmap (hooks in v1, features if time permits)

**Hooks built in v1 (about a day of extra work):**

1. Internal event bus in the agent. Everything (user message, device state change, perception event, node health) is an `Event`.
2. Verify step after `set_state`: ack plus read-back compared with the expected value.
3. Action log with `reason` and `trigger` fields.
4. Executor separate from planner. Planner output = a list of validated tool calls.
5. Per-request budgets (max steps, time, retries).
6. `needs_confirmation(action, trigger)` policy function (reuses `risk`). For a user-initiated trigger it yields a `confirmation_request`; for any proactive or automation trigger on a `risk: confirm` endpoint it yields `notify_only` (never a prompt, never an execution).
7. Empty `rules` table and a stub rules engine that subscribes to the bus.

**Later, in this order (cut from the bottom if time runs out):**

1. **Compiled automations:** the LLM turns a sentence into a trigger → condition → actions rule (same action format as scenes); the rules engine runs it with no LLM at runtime. Saved only after user approval.
2. **Multi-step goals:** one constrained-JSON plan (≤4 steps), run through the existing executor, replan once on failure.
3. **Proactive perception:** YOLO change → VLM check → notify or act.
4. **Routine mining:** propose automations from repeated patterns in the action log (optional).

Autonomy: low risk and high confidence acts and notifies; medium or ambiguous asks first; `risk: confirm` always asks the user who is interacting (never auto-retried); proactive and automation-triggered actions on `risk: confirm` endpoints never prompt and only notify. Proactive actions are rate-limited, undoable where possible and always logged with a reason. With only v1 built, describe the system as an LLM-driven multimodal control framework with an agent loop; with closed-loop verification and compiled automations the "agentic" claim is stronger.

---

## 10. Phases and evaluation

| Phase | Weeks | Outcome |
|---|---|---|
| P0 | days 1-3 | Contracts frozen; Tailscale, TLS broker with dynsec, repo up; BLE + TLS/NTP spike |
| P1 | 1-2 | Device layer: device-hub, firmware core, simulator, one real node provisioned over BLE |
| P2 | 2-3 | **First demo:** text in the dashboard → LLM → real device |
| P3 | 3-5 | Voice, vision node, YOLO on Jetson (with and without camera); measure LLM latency, decide 7B vs 3B |
| P4 | 4-6 | Scheduler, fast path, scenes, async vision, events, memory, multi-user |
| P5 | 5-7 | Flutter app, dashboard polish, admin flows, agentic extras |
| P6 | 6-8 | Evaluation, enclosure thermals re-check, report |

**Evaluation tables for the report**

1. Routing ablation: always-LLM vs always-cheapest vs cost-aware, on 100+ commands (single, multi-device, scenes, ambiguous, invalid, vision).
2. Constrained decoding: VLM JSON validity 0% → 30/30 [measured on PC; repeat on the Pi]; LLM actions unconstrained vs registry-constrained (invalid-endpoint execution rate).
3. Fast path vs LLM path latency and accuracy.
4. STT: whisper base.en on the Pi (latency, intent accuracy) with real mic and Android recordings, vocabulary prompt and `-ac 512` variants.
5. Vision: VLM and YOLO on real camera images (LED, meter, switchboard, lights on/off); Jetson vs Pi YOLO latency.
6. System: RAM (MemAvailable), CPU, temperature and power per node; behavior with no camera, Jetson off, vision Pi off, device removed mid-run.
7. Modularity: provision, rename and forget a device live with no restart or code change; forget really cuts off MQTT access; scene with selector picks up a new light automatically.
8. Security checks: a node cannot publish to another node's topics; a revoked node cannot reconnect; the LLM cannot call any admin operation.

---

## 11. Risks and open items

| Risk | Mitigation |
|---|---|
| LLM turn latency 15-30 s [est], cold prefix minutes | Fast path, cached static prefix, tiny prompt, async responses with a status message, optional 3B model |
| Multi-user history thrashes the KV cache | History only in the prompt tail; single slot; short history plus summaries |
| BLE provisioning with a custom data channel may not be exposed by the Arduino-ESP32 wrapper | First spike: try the Espressif protocomm custom endpoint; else a small custom GATT service with an encrypted payload |
| BLE "just works" pairing has no MITM protection | Short button-triggered window in v1; PoP field reserved and added later; document the limitation in the report |
| TLS needs a valid clock (NTP) and RAM on the ESP32 | NTP before MQTT; verify behavior without internet (internal NTP on the Pi or a long-lived cert policy); measure heap during the handshake |
| Web Bluetooth only on Chromium over HTTPS | `tailscale serve` for HTTPS; Flutter app as the fallback |
| Mosquitto dynsec adds an admin-plugin dependency | Hub wraps it in one module with tests; spike it in P0 |
| Whisper and VLM contend on the 4GB Pi | Priority queue; re-measure together; MemAvailable guard |
| Pi VLM thermals (78-80 °C stock fan [measured]) | Re-check in the final enclosure; add a heatsink/fan |
| Jetson software is old (py3.6, TRT 8.2), 16 GB eMMC | Build the engine with `trtexec`; keep the service light or use an L4T container; models/logs on the 128 GB SD card |
| ESP32 Wi-Fi drops | FreeRTOS reconnect task with backoff, LWT, retained state, ack timeouts |
| Unsafe actions | Registry validation, `risk: confirm`, roles, firmware-side safety (boot/failsafe/max_on), audit log |
| Prompt injection through device-supplied text | Names and rooms are admin-set; device strings sanitized and never placed raw in prompts |
| Scope | Contracts frozen early; eval tables defined now |

Open items: the real device list (decided later; the design does not depend on it); 7B vs 3B (decide after phase 3 measurements); image sources for the live demo; OTA (out of scope, hook reserved); PoP for BLE (later).
