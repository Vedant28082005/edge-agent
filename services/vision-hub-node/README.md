# vision-hub-node (vlmpi: Raspberry Pi 5, 4 GB, no Docker)

Not the same as `services/vision-node` (pi-vision, contract `vision-node-internal.yaml`, agent-facing, Docker). This one runs
on the **vlmpi** from images only (a camera can be added later), and in Phase 2 joins the Edge Home hub as an MQTT node.

## Phase 1: local vision service (`vision_hub`)

HTTP on **127.0.0.1:8310 only** (no auth because nothing but local processes can reach it). It wraps llama-server
(Qwen3-VL-2B Q4_K_M + mmproj Q8_0, 127.0.0.1:8080, `-c 1024 -np 1 -t 4`, `cache_prompt:false`, temperature 0).

| Call | Returns |
|---|---|
| `POST /v1/yesno?question=...` + image body | `{"answer": bool, "ms": int}` |
| `POST /v1/scene` + image body | `{"person": bool, "objs": [<=3 strings], "ms": int}` |
| `GET /health` | `{"status": "ok"/"degraded", "vlm": bool, "busy": bool, "source": ..., "last_ms": ...}` |

Image input: raw body (`Content-Type: image/jpeg|png|webp`), or JSON `{"path": "rel/file.jpg"}` (must resolve inside
`--allowed-dir`, symlinks and `..` rejected), or JSON `{"frame": true}` (next frame of the configured **FrameSource**).
Errors are `{"error": code}` with fixed codes: `bad_request` 400, `payload_too_large` 413, `bad_image` 422, `busy` 429
(+`Retry-After`), `model_error` 500, `schema_violation` 502, `vlm_unavailable` 503, `timeout` 504.

Rules enforced: max image 5 MiB and 4096 px / 16 MP (header check), PNG/JPEG/WebP by magic bytes, resize to **224 px long
side** in RAM, **one request at a time** (a busy node answers 429 *before* reading the body), per-call timeout (30 s),
**frames are never written to disk** and never logged.

**VLM output is untrusted.** yes/no is grammar-constrained to `yes|no` and anything else is a 502. The scene call uses a JSON
schema; the result is re-validated and every label is lower-cased to `[a-z0-9 -]`, <= 24 chars, <= 3 words, de-duplicated
(case and plural insensitive), capped at 3, and **dropped if it looks like an instruction** (ignore/unlock/system/http/...).
`lights` is deliberately not exposed (unreliable per the benchmark). The model's system prompt is constant and the caller's
question is cleaned (control chars removed, `<` `>` stripped, 200 chars) and only placed in the user turn as labelled data.
Model text is never forwarded raw.

### FrameSource

`vision_hub/frames.py`: anything with `async grab() -> bytes` and `describe()`. `FolderSource` (cycles a folder, or one file)
is implemented; `make_source("usb:0")` / `"picam"` currently raise `source_not_implemented`, so a camera class plus one
line in `make_source` is the whole change later.

### Run / test on the PC

```powershell
tools\vision-hub-node-test.ps1        # venv, pytest (fake llama-server, no model), contract tests
```

### Real smoke test on the vlmpi (needs llama-server + service running)

```bash
~/edge-vision/venv/bin/python ~/edge-vision/app/tools/smoke.py            # 3 synthetic images
~/edge-vision/venv/bin/python ~/edge-vision/app/tools/smoke.py a.jpg b.jpg c.jpg   # your own photos
```

Unit files are in `deploy/systemd/` (user units; installed by the Phase 3 kit, not yet). `llama.env.example` shows the three
paths to adjust.
