# BLE provisioning contract (v1)

Implemented once each in firmware, Flutter (`flutter_blue_plus`) and the web client (Web Bluetooth). Admin-initiated: the gateway (`POST /admin/provision-sessions`, admin role) creates the node's MQTT user and returns the **bundle**; the client relays it over BLE.

**Status: DRAFT pending the S2b spike.** The GATT layout is stable; the session encryption below (X25519 ECDH + HKDF + AES-256-GCM) is **DRAFT** and may be replaced by Espressif protocomm security or another scheme after the spike. If the Arduino-ESP32 wrapper cannot expose a custom data channel via Espressif protocomm, this custom GATT service is the fallback and is the contract to implement. Any change after the spike bumps `proto`.

## Window

- Board advertises only while in its **provisioning window**: first boot with no config, or BOOT button held 3 s.
- Window = **5 minutes**, then BLE turns off (idle until button or power-cycle). A successful provisioning ends the window immediately.
- Advertised name `Setup-XXXX` (last 4 hex of node_id), advertising the service UUID below. LED pattern while open.

## GATT service

Service UUID: `4dd57997-6f7e-4f0a-b1d5-e01ef6674efe` (random 128-bit v4, generated once for this project; do not change without bumping `proto`).

| Characteristic | UUID | Properties | Content |
|---|---|---|---|
| `info` | `9809777c-4f37-44e6-b054-b3fb7ee9f6db` | read | UTF-8 JSON, plaintext, see below |
| `session` | `8583a1b2-4f51-48cc-8270-35f88e7b7260` | write, indicate | key exchange, see below |
| `data` | `c5a63a5d-8766-4e3b-b2b2-eab7c732361b` | write (no response), notify | chunked encrypted bundle (client→board); chunk acks (board→client) |
| `result` | `43f00218-77e0-4e21-9eb2-56da36658cee` | read, notify | UTF-8 JSON status |


### info (read)
```json
{"proto": 1, "node_id": "esp32-a1b2c3d4e5f6", "hw": "esp32-devkit", "fw": "0.1.0",
 "window_left_s": 241, "pop_required": false}
```
Contains nothing secret. Strings are length-limited and treated as untrusted by clients.

### session (write, indicate) [DRAFT]
1. Client writes: `{"proto":1, "client_pub":"<base64 X25519 public key>", "pop": ""}`.
   `pop` is the **reserved PoP field**: present, empty, ignored by v1 firmware. When PoP is added it is mixed into the key derivation (below) and `info.pop_required` becomes true.
2. Board indicates: `{"board_pub":"<base64 X25519 public key>", "salt":"<base64 16 bytes>"}`.
3. Both derive `key = HKDF-SHA256(ikm = X25519 shared secret, salt = salt, info = "edge-agent-ble-v1" || pop, len = 32)`.
   One session per window attempt; a new `session` write discards the previous key and any partial data.

**Security limitation (accepted in v1): without the PoP this scheme does NOT protect against an active man-in-the-middle.** Unauthenticated ECDH gives confidentiality against passive sniffing only; an attacker in radio range during the 5-minute window who relays or impersonates the board can read the bundle (Wi-Fi password, MQTT credentials). The physical button window is the only trust anchor until the PoP (printed secret / QR, mixed into the key derivation) is added. Do not describe the provisioning as authenticated in the report.

### data (chunked encrypted bundle) [DRAFT encryption]
Plaintext bundle (UTF-8 JSON, max 4096 bytes):
```json
{"v": 1,
 "wifi": {"ssid": "...", "password": "..."},
 "mqtt": {"host": "192.168.1.10", "port": 8883, "username": "esp32-a1b2c3d4e5f6", "password": "..."},
 "ntp_host": "192.168.1.10",
 "ca_pem": "-----BEGIN CERTIFICATE-----\n..."}
```
`ntp_host` optional: the LAN NTP server the board should use first (chrony on `pi-brain`, see `docs`/`deploy/README.md`). The firmware falls back to `pool.ntp.org` if `ntp_host` is absent or unreachable. If the board has no internet and chrony is down, TLS cannot validate the certificate dates. The bundle contains secrets: never logged, never stored anywhere but NVS.

Encryption: AES-256-GCM with the session key, random 12-byte nonce. Transmitted blob = `nonce(12) || ciphertext || tag(16)`. AAD = `node_id` UTF-8.

Chunking: the blob is split into chunks that fit `MTU-3-4` bytes. Each write to `data`:
```
[ seq : u16 big-endian ][ flags : u8 ][ len : u8 ][ payload : len bytes ]
flags: bit0 = first chunk, bit1 = last chunk
```
`seq` starts at 0 and increments by 1. The board notifies on `data` after each chunk: `[seq u16][status u8]` (0 = ok, 1 = out-of-order, 2 = too large). On `last`, the board decrypts and validates the bundle; a failed tag check yields result `bad_bundle`. Total size limit 4096 + 28 bytes. Abort on 30 s of inactivity.

### result (read, notify)
```json
{"state": "idle|session|receiving|stored|error", "code": "ok|bad_session|bad_chunk|bad_bundle|window_closed|storage_failed"}
```
`stored` means the bundle is in NVS. The board then turns BLE off, reboots, joins Wi-Fi and connects over TLS. **Wi-Fi/MQTT success is not reported over BLE**; the hub sees the announce and matches it to the pending session.

## Sequence

1. Client scans for the service UUID, lists `Setup-XXXX`; admin picks one; client writes nothing yet. Board blinks its LED (the client may trigger it by connecting; there is no `identify` over BLE in v1).
2. Client reads `info` (node_id).
3. Client calls `POST /admin/provision-sessions {node_id}`; gets the bundle (Wi-Fi from the admin or a saved profile).
4. Client does the `session` handshake, sends the chunks on `data`, waits for `result.state == stored`.
5. Board reboots into normal mode. If no announce within about 10 minutes the gateway deletes the pending session and MQTT user.

## Unprovision

Over MQTT (`cmd: unprovision`) or by holding the BOOT button for 10 s: wipe NVS config, return to provisioning mode.
