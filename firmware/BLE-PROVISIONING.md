# BLE provisioning (S2b): implementation notes, client payload, security decisions

Contract: `contracts/ble-provisioning.md` (proto 1, **DRAFT**, unchanged by S2b). This file records what was built, the exact bytes a client
writes, the decisions taken where the contract is silent, and the proposed contract changes (not applied, see "Contract gaps").

Labels: **[built]** compiled/tested in this repo, **[sim]** only simulated, **[unverified]** written but never run on a board.

## What exists

| Part | Where | Status |
|---|---|---|
| State machine + protocol (window, PoP, lockout, chunking, bundle checks) | `esp32-core/src/edge/BleProv.{h,cpp}` | [built] host-tested (`test/test_ble_prov`, 21 tests) with a FAKE crypto |
| Button hold detector (3 s open, 10 s factory reset) | `HoldDetector` in `BleProv.h` | [built] host-tested |
| NimBLE GATT server, mbedtls X25519/HKDF/AES-GCM, PoP in NVS, BLE task, serial `pop set` | `BleProvEsp.{h,cpp}`, `EdgeNodeEsp.cpp` | compile status: see the report; **never run on a board** [unverified] |
| Board + client in Python, fake GATT peer (loopback TCP), `--ble-provision` sim mode | `tools/sim-esp32/simesp32/{ble,bleprov,provclient}.py` | [sim] 14 unit tests, real X25519/HKDF/AES-GCM via `cryptography` |
| End-to-end step (session -> payload -> node connects -> announce -> pending) | `tools/e2e-smoke.ps1` ("BLE:" steps) | [sim] needs Docker |

The Python board mirrors the C++ rule for rule, but the C++ crypto glue (mbedtls) and the Python (`cryptography`) have **not** been
cross-tested against each other: first hardware contact is the interop test (checklist below).

## Security model

1. **Window.** BLE is on only (a) at boot when the node has **no valid stored config** (first boot, after factory reset/unprovision), or (b) after the
   physical provisioning button (default GPIO0 = BOOT) is held 3 s. Window = 5 min, then BLE is off (`window_closed`); the node keeps running local
   controls and failsafe timers; it waits for the button or a power cycle. Success ends the window at once.
2. **Proof of possession (PoP).** Each board has a 128-bit secret (26 chars of `[A-Z2-7]`, the label/QR), stored once in NVS namespace `edgeid` via the
   serial shell (`pop set <code>`; `pop status` prints only set/unset). It is mixed into the key derivation (`HKDF info = "edge-agent-ble-v1" || pop`,
   exactly the reserved field of the contract) and **never sent over the air** (a non-empty `pop` in the `session` write is rejected). A wrong PoP derives a
   different key, the GCM tag check fails, and the attempt is counted. This authenticates the exchange against an active MITM and defeats offline guessing
   of a captured transcript only because the PoP has 128 bits; **do not shorten it to a human-typeable code** (a 6-digit PoP is brute-forced offline in
   seconds from one captured session).
3. **Lockout.** 5 failed attempts (tag failures) lock BLE for 5 min; the button cannot skip the cool-down. After it: an unprovisioned board opens a fresh
   window, a configured board stays off until the button. The counter is RAM-only (a power cycle resets it; an attacker in radio range cannot power-cycle).
   Content errors from an *authenticated* sender (wrong node_id, bad JSON) are not counted.
4. **Default is secure:** `ProvPolicy.requirePop = true`: a board with no PoP **does not open BLE at all** (serial log says so). Only a dev policy
   (`EdgeNode::provisionPolicy`) turns this off, giving the contract's unauthenticated v1 (passive-sniff protection only).
5. **Secrets.** Bundle, key, PoP and Wi-Fi/MQTT passwords are never logged, never returned over BLE (`result`/`info` carry no secret), never in advertising
   data (name `Setup-XXXX` = last 4 hex of the node_id + service UUID). Key, partial blob and plaintext are zeroed after use; the node stores to NVS only after
   the GCM tag, JSON, `mqtt.username == node_id`, field-format and "no PRIVATE KEY in ca_pem" checks pass.
6. **Timeout / factory reset.** Window timeout: BLE off, nothing stored, LED back to the 2 Hz "unconfigured" pattern (4 Hz while the window is open).
   Idle transfer (30 s without a chunk): session dropped, window stays open. Factory reset (button 10 s, or MQTT `cmd: unprovision`): wipe NVS config
   (`edgecfg`), **keep the PoP** (`edgeid`), restart, window opens (no creds). The gateway side of unprovision (MQTT user deleted) is unchanged.
7. **Hub binding.** The admin provision session (TTL 600 s) is created by the gateway; the node cannot verify it, and the hub cannot know the PoP (the PoP
   is on the board's label, the hub never sees it). So the binding is: the *client* holds both (label PoP + hub bundle), and the node accepts only a bundle
   whose `mqtt.username` equals its own node_id. A bundle that expires unused is cleaned up by the hub (MQTT user deleted after 600 s without an announce).

## The exact client flow (dashboard / Flutter / Web Bluetooth)

Service `4dd57997-6f7e-4f0a-b1d5-e01ef6674efe`; characteristics `info 9809777c-…`, `session 8583a1b2-…`, `data c5a63a5d-…`, `result 43f00218-…`
(full UUIDs in the contract). Advertised name `Setup-XXXX`.

1. Scan by service UUID; the admin picks a board. Connect. Request MTU (>= 64; chunk size below must be <= MTU-3-4).
2. Read `info` -> `{"proto":1,"node_id":"esp32-…","hw":…,"fw":…,"window_left_s":N,"pop_required":true}`. Treat every string as untrusted. The admin supplies the
   board's **PoP** (scan the label QR / type the 26 characters). Keep it in memory only.
3. `POST /admin/provision-sessions` (gateway, admin token) with `{"node_id":"<from info>","wifi":{"ssid":"…","password":"…"}}` (or `wifi_profile`) -> `{"session_id","expires_at","bundle":{…}}`.
   The bundle contains secrets: never log it, never put it in a URL.
4. Generate an X25519 key pair. Subscribe to `session` (indications), `data` (notifications), `result` (notifications). **Write `session`:**
   `{"proto":1,"client_pub":"<base64 32 bytes>","pop":""}` (UTF-8 JSON; `pop` must be the empty string). The board indicates
   `{"board_pub":"<base64 32>","salt":"<base64 16>"}`.
5. `shared = X25519(client_priv, board_pub)` (reject an all-zero result); `key = HKDF-SHA256(ikm = shared, salt = salt, info = UTF8("edge-agent-ble-v1") || UTF8(pop), L = 32)`.
6. `plain = UTF-8 JSON of the hub's bundle` (<= 4096 bytes, compact separators):
   `{"v":1,"wifi":{"ssid","password"},"mqtt":{"host","port","username","password"},"ntp_host","ca_pem"}`.
   `blob = nonce(12 random) || AES-256-GCM(key, nonce, plain, aad = UTF8(node_id)) = ciphertext || tag(16)`; `<= 4124` bytes.
7. Split `blob` into chunks (<= 180 bytes recommended) and **write each to `data` (write without response)**, waiting for the notification before the next:
   `[seq u16 BE][flags u8: bit0 first, bit1 last][len u8][payload]`, `seq` from 0. Board notifies `[seq u16 BE][status u8]` (0 ok, 1 out-of-order, 2 too large).
8. After the last chunk read/await `result`: `{"state":"stored","code":"ok"}` = in NVS, the board turns BLE off, restarts and joins Wi-Fi. Any other `code`
   (`bad_bundle` = wrong PoP or tampering and counts toward the lockout; `bad_session`, `bad_chunk`, `window_closed`, `storage_failed`) is an error: show it, do **not** retry
   blindly (5 wrong PoPs lock the board for 5 min). Success on Wi-Fi/MQTT is **not** reported over BLE.
9. Poll `GET /admin/nodes` (or the WebSocket `pending_node`/`registry_changed`): the node appears with `status: pending` once it has announced; approve it as usual.
   No announce within ~10 min: the hub removes the MQTT user; start again from step 3.

Reference implementation: `tools/sim-esp32/simesp32/ble.py` (`BleClient.provision`) and the CLI `simesp32.provclient`. WebCrypto supports X25519 (Chrome 133+/Safari 17+),
HKDF and AES-GCM natively, so a Web Bluetooth client is a direct port; Flutter needs `cryptography` or `pointycastle`.

**Dashboard (Web Bluetooth) was not added:** it needs a secure context (the dashboard is served over Tailscale HTTPS, fine), Chrome-only support, and a mock
for tests; the whole flow is specified above for S9 (Flutter, the supported path) and a Web Bluetooth page can be added later without any firmware change.

## Contract gaps (not edited; proposals for the owner)

`contracts/ble-provisioning.md` v1 defines the GATT layout, chunking, bundle and a *reserved* `pop` mixed into the KDF. It does **not** define:

1. **What the PoP is, its format/entropy and where it comes from.** Implemented: 26-char base32 (128 bit) from the label, stored via serial/factory tool. Propose: state this, mandate >= 80 bits, define the QR payload (e.g. `edge1:<node_id>:<pop>`), and set `pop_required` semantics (`true` => the client must derive with the PoP).
2. **Lockout.** No `locked` result code and no cool-down rule. Implemented: 5 failures / 5 min; the board reports `window_closed` while locked. Propose: add `locked` to `result.code`, add `retry_after_s` to `info`.
3. **`result.code` for a wrong PoP.** Indistinguishable from tampering: reported as `bad_bundle` (GCM failure). Propose: keep this deliberately (no oracle) and document it.
4. **Hub-bound one-time code.** The request asked for a code bound to the hub's provision session. Not possible without a channel from hub to board before provisioning (the board is offline and has no display). Proposal: if wanted, the *hub* mints a session nonce that the client mixes into the AAD (`aad = node_id || session_id`); the board cannot verify it, so it adds nothing against an attacker, only against replay of an old encrypted bundle. Not implemented.
5. **Factory-reset behaviour for the PoP** (kept across wipe) and `requirePop` default (BLE refuses to open without a PoP): needs to be written down.
6. **Window after lock for an already-configured node** (stays off until the button).
7. Hub: `ProvisionSession` has no field for the PoP or the Wi-Fi-profile resolution result; nothing is required there (the PoP never reaches the hub).
8. MTU: the contract says chunks "fit MTU-3-4"; nothing says the client must negotiate a larger MTU. Implemented client uses <= 180 bytes.

## Manual hardware test checklist (first real ESP32)

Prereqs: devkit, USB, PC with Bluetooth (for `bleak`, optional) or an Android phone with the S9 app, dev broker + hub + gateway running, an admin token.

1. `pio run -e light` builds; flash **by hand** (this repo never uploads). Open the serial monitor at 115200.
2. Boot log: `no provisioning PoP stored: BLE provisioning stays OFF`. Confirm **no** `Setup-XXXX` device is visible in a BLE scanner (nRF Connect).
3. `pop status` -> `pop: unset`. `pop set <26-char code>` -> `ok`; `pop status` -> `pop: set`; a second `pop set` is refused. The code must not appear anywhere in the log. Reset the board.
4. LED blinks 4 Hz; `Setup-XXXX` is visible; the advertisement shows the service UUID and no node_id/secret. Read `info`: `pop_required:true`, `window_left_s` counts down from 300.
5. Do nothing for 5 min: advertising stops, LED goes 2 Hz, `result.code = window_closed` is not readable (BLE off). Hold the button 3 s: advertising returns.
6. Wrong PoP x5 (`provclient`/app with a different code): each returns `bad_bundle`; the 5th locks: no advertising for 5 min, button hold does nothing. Nothing stored (`status` over serial shows config incomplete).
7. Correct PoP: `result stored`, board restarts, joins Wi-Fi, TLS-connects (NTP first), announces; `GET /admin/nodes` shows it `pending`. Check the hub log and the board serial log contain no password.
8. Interop checks (the unverified part): a successful run proves C++ mbedtls X25519/HKDF/GCM == Python `cryptography`. If step 7 fails with `bad_bundle` on the *right* PoP, suspect the X25519 byte order in `BleProvEsp.cpp` (`reverse32`) first, then the HKDF `info` bytes.
9. Power-cycle with creds: BLE stays off. Hold 3 s: window opens; provisioning again replaces the creds (hub: rotated password) - confirm the old broker password stops working.
10. Hold the button 10 s: config wiped, board restarts into the window, PoP still set (`pop status`).
11. MQTT `unprovision` (admin UI): board wipes, restarts into the window.
12. Walk away from the phone mid-transfer for 30 s: `bad_chunk`, window still open. Disconnect mid-transfer: session dropped.
13. Heap: `RUN (heap free …)` after provisioning vs before; NimBLE is deinitialised (`ble: off`) so the heap should recover.
