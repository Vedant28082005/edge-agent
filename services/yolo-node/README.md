# yolo-node

Object detection node implementing `contracts/openapi/yolo-node-internal.yaml` (contract 1.0.0). Port 8400, caller: the agent only (scope `agent`).

| Operation | Notes |
|---|---|
| `GET /health` | open; `ok=false` until the engine is loaded |
| `POST /v1/detect` | multipart `params` (JSON) + `image`; jpeg/png/webp by magic bytes, 5 MiB, 4096 px / 16 MP; RAM only |
| `POST /v1/detect/snapshot` | `{source_id}` of a **configured** source only (never a URL/path); 1 snapshot/s/source |
| `GET /v1/sources` | read-only; empty without a camera |

Auth: `X-Service-Secret` on everything except `/health`, checked before routing and before any body byte is read, against a SHA-256 verifier file (`verifier_file`, constant-time). No verifier, no start. `--insecure-no-auth` only with `--mock`. Binding `0.0.0.0` is refused unless `allow_wildcard_bind` (container only; compose publishes on the Tailscale address).

Scheduling: one inference at a time, 8 waiting, then 429 `queue_full` + `Retry-After`; per-request deadline `timeout_ms` (cap 25 s) covers queue wait; a job still queued at its deadline is dropped unrun (504). Image bytes are never written, cached or logged (logs: operation, status, ms, queue depth). Labels must match `[A-Za-z0-9 _-]{1,32}` and, if `labels_allow` is set, be in it; the response is rebuilt field by field (closed schema).

## Backends (`backend` in the config)

| | |
|---|---|
| `mock` | deterministic, no model; `--mock` also adds a synthetic camera `cam0` |
| `onnx` | onnxruntime CPU on a YOLOv8 `.onnx` (Pi 5, PC). Export on the PC: `yolo export model=yolov8n.pt format=onnx opset=12 imgsz=640` (ultralytics is an export-time tool, not a runtime dependency) |
| `trt` | Jetson Nano, JetPack 4.6. **Python 3.6 constraint:** the `tensorrt`/`pycuda` bindings exist only for the system Python 3.6, which cannot run this service's dependencies. So the engine runs in an isolated child process, `backends/trt_worker.py` (3.6 syntax, only numpy + tensorrt + pycuda, never sees files or image bytes, only a float tensor over a pipe); the service itself runs in a Python 3.8 venv. Build the engine on the Jetson: `trtexec --onnx=yolov8n.onnx --fp16 --saveEngine=yolov8n-fp16.engine` (ONNX opset <= 12 for TRT 8.2). **Not compiled or run here.** |

Sources (`sources` in the config, max 8): `usb`/`csi`/`rtsp` (OpenCV, optional dependency, newest frame only), `http_snapshot` (URL from the config; keep credentials out of git), `folder_replay` (test images).

## Run / test

```powershell
tools\yolo-node-test.ps1                      # all tests, no hardware
.venv\Scripts\python.exe -m yolo_node --mock --insecure-no-auth --port 8400   # from services\yolo-node
```

Deploy: Pi/PC CPU image `deploy/docker/Dockerfile.yolo-node` + `deploy/compose.yolo-node.yml` (uid 10104, read-only fs, cap_drop ALL, no file written); Jetson native `deploy/systemd/yolo-node.service` + `deploy/config/yolo-node.jetson.json` (set `host` to the Jetson's real Tailscale address; the shipped one is a placeholder).

Secret (CLAUDE.md rule 10): generate `yolo-node-api.secret` on pi-brain (owner `edge-agent`, mode 600, 256-bit, read only by the agent). Copy only its hex SHA-256 to the node as `/etc/edge-agent/secrets/verifiers/agent.sha256` (mode 600, owner = the node's service user). `setup-pi-brain.sh` does not create this secret yet.

## What only the real Jetson can verify

- `trt_worker.py` against TensorRT 8.2 / pycuda / CUDA 10.2 (binding names, FP16 I/O dtype handling, `execute_async_v2`), engine load time and the `start_timeout_s` default.
- Python 3.8 venv install (aarch64 wheels for numpy/Pillow on JetPack 4.6) and the worker running under system Python 3.6.
- Latency, RAM and power of YOLOv8n FP16 (all `[est]` until measured), accuracy parity with the ONNX export, thermal throttling.
- CSI/USB camera via the JetPack OpenCV (GStreamer pipeline strings), `frame_age_ms` with a real stream, `/dev/video0` access under the systemd sandbox.
- The systemd hardening with CUDA device nodes; Tailscale bind ordering at boot.
- `mem_available_mb` and `temp_c` (`/proc/meminfo`, thermal_zone0) read on the board; the `onnx` backend speed on the Pi 5.
- Not covered even on the PC: arm64 image build/smoke (no QEMU run here), the continuous camera loop that publishes `perception/{node}/events` over MQTT (a separate component, not part of this API).
