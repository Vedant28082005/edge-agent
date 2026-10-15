# vision-node (pi-vision, port 8300)

Implements `contracts/openapi/vision-node-internal.yaml` (v1.0.0) **exactly**: `GET /health` (open) and, behind
`X-Service-Secret`, `POST /v1/vlm/yesno`, `/v1/vlm/short`, `/v1/vlm/json`, `/v1/stt`, `/v1/detect`. The agent is the only
caller. The node reaches **llama-server (Qwen3-VL-2B + mmproj)** and **whisper.cpp (base.en)** over loopback; clients never do.

## Run (dev PC, no models)

```powershell
tools\vision-node-test.ps1            # venv, 140 tests, real-socket smoke   (-Image: also arm64 image + mock health, needs Docker)
.venv\Scripts\python.exe -m vision_node --mock --insecure-no-auth --port 8300   # from services\vision-node, PYTHONPATH=.
```

`--insecure-no-auth` is accepted only with `--mock` and logs a warning on every start. Without it the node refuses to start
unless `/etc/edge-agent/secrets/verifiers/agent.sha256` (hex SHA-256 of the agent's `vision-node-api.secret`) exists.
Production refuses a wildcard bind: `--host` / `http.host` must be the Tailscale address.

## Layout

| Module | Role |
|---|---|
| `app.py` | routes, request pipeline, response closing + conformance backstop (every response re-validated against the contract) |
| `contract.py` | loads the contract YAML; params and results are validated with ITS schemas (`$` patterns made strict: `\Z`) |
| `multipart.py`, `media.py` | in-RAM multipart parser (no temp files), magic-byte sniffing, header-only size/pixel checks, audio to 16 kHz WAV (ffmpeg via memfd/pipe) |
| `scheduler.py` | one heavy job at a time, 4 waiting, STT first, deadlines include queue wait; admission before the body is read |
| `schema.py` | BoundedSchema -> JSON schema for grammar-constrained decoding; strict output validation |
| `sanitize.py` | ASCII allowlist, question cleaning, STT junk filter |
| `backends.py` | `VlmBackend` / `SttBackend` / `DetectBackend` protocols + mocks. `real.py`: llama-server, whisper.cpp, ONNX YOLOv8n |
| `cache.py` | 30 s cache keyed by a hash, stores the answer only |

## Decisions worth knowing

* **No persistence/logging of content.** The body is parsed from RAM (Starlette's form parser would spool to temp files).
  Logs carry operation, status, latency, queue depth; an unexpected exception logs its **class name only**. `cache_prompt:false`
  to llama-server; the systemd units discard stdout (and whisper's stderr).
* **Queue.** `admit()` rejects with 429 *before* reading a body when 4 jobs already wait (or 7 requests are in flight), so uploads cannot pile up in RAM.
  A slow uploader is cut off after 20 s (504 `timeout`).
* **yes/no** is grammar-constrained (`yes|no|unsure`); anything else (after one retry) is 502 `schema_violation`.
  **JSON** is decoded with `response_format: json_schema` (all declared fields required by the grammar), then validated again against the *request* schema
  (exact field set, types, bounds, enums, lengths, strings reduced to `SafeValue`); one retry at temperature 0.3, then 502.
* **Question as data.** Control/format characters removed, `<` `>` replaced (a chat-template token can not be formed), placed only in the user turn behind a fixed label; the system prompt is constant.
* Unknown routes answer 404 `{"error":"bad_request"}` only after authentication (outside the contract's operations; chosen because the error enum has no `not_found`).
* `truncated` on `/v1/vlm/short` is also set when the node cut the text to 240 characters.
* YOLO labels come from the fixed COCO table (spaces, e.g. `traffic light`); every label is re-checked against `^[A-Za-z0-9 _-]{1,32}$`.

## Deploy (pi-vision)

`deploy/compose.vision.yml` (host network, binds `VISION_BIND` only), `deploy/systemd/vision-llama-server.service`,
`vision-whisper-server.service` (+ `*.env.template`), `deploy/config/vision-node.json`, `deploy/docker/Dockerfile.vision-node`.
See `deploy/README.md` ("pi-vision"). Build: `tools\build-images.ps1 -Platform arm64 -Tag v1 -Save`, ship: `tools\deploy.ps1 -Target vision -Images -Tag v1 -Up`.

## Not verified here (needs the real Pi 4GB, real models, Docker)

See the report in the session summary; in short: RAM with VLM + whisper + node loaded together, real llama-server/whisper.cpp
request shapes and flags, ffmpeg decoding of real webm/m4a/aac, YOLO ONNX accuracy/latency, arm64 image build, all latencies `[est]`.
