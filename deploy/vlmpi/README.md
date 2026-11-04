# vision-node on the vlmpi, native (no Docker, VLM only)

Runs the existing `services/vision-node` (contract `vision-node-internal.yaml` 1.0.0, unchanged) as a venv + two systemd **user**
units on the vlmpi (Pi 5, 4 GB, Debian 13, Python 3.13): `llama-vision` (llama-server, Qwen3-VL-2B Q4_K_M + mmproj Q8_0,
`-c 1024 -np 1 -t 4`, 127.0.0.1:8081) and `vision-node` (listens on the Tailscale address 100.122.169.70, port 8300,
`X-Service-Secret`). Whisper and YOLO are switched off (`"whisper": {"enabled": false}`, `"yolo": ...`): their operations answer
503 `vision_unavailable` and `/health` reports only the VLM. Images are downscaled to 224 px (`limits.vlm_max_side`) before
llama-server. `MemoryMax` values are `[est]`. Logs: llama-server runs with `--log-disable` and stdout discarded; vision-node logs
WARNING and above only (journald on the SD card stays tiny).

`llama-vision.service` is rendered by `install.sh` from `llama-vision.service.template`: systemd does not expand `${VAR}` in the
first word of `ExecStart`, so the llama-server path from `~/edge-vision/config/llama.env` (`VLM_BIN`) is written in literally.
`install.sh` refuses a `VLM_BIN` that is not an absolute, executable path.

## Steps (from the PC, one at a time; only the sudo prompts need passwords)

1. Ship the code and run the no-sudo installer (starts nothing, sends no secret; re-run it any time, it keeps your config,
   `llama.env` and verifier, and writes `vision-node.json.new` if the template changed):
   ```powershell
   tools\vlmpi-deploy.ps1
   ```
2. **The API secret, without it ever being shown.** It is created on the brain (file owned by `edge-agent`, mode 600, the same
   convention as `tools/setup-pi-brain.sh`). Only its SHA-256 (the verifier) goes to the vlmpi. A: create the secret if it does not exist
   and leave the verifier in a temporary file in your brain home (sudo password prompt):
   ```powershell
   ssh -t homebench@100.82.120.100 "sudo test -s /etc/edge-agent/secrets/vision-node-api.secret && echo 'secret exists, keeping it' || { openssl rand -base64 32 | tr -d '\n' | sudo tee /etc/edge-agent/secrets/vision-node-api.secret >/dev/null; sudo chown edge-agent:edge-agent /etc/edge-agent/secrets/vision-node-api.secret; sudo chmod 600 /etc/edge-agent/secrets/vision-node-api.secret; echo 'secret created'; }; sudo cat /etc/edge-agent/secrets/vision-node-api.secret | openssl dgst -sha256 -r | cut -d' ' -f1 > ~/.vn-verifier; chmod 600 ~/.vn-verifier; echo verifier-chars: `$(wc -c < ~/.vn-verifier)"
   ```
   It should end with `verifier-chars: 65` (64 hex plus a newline). B: copy the verifier brain to vlmpi through this PC without writing
   it to disk here (it is a hash, not the secret, and nothing is printed):
   ```powershell
   scp -3 homebench@100.82.120.100:.vn-verifier vlmpi@100.122.169.70:edge-vision/secrets/verifiers/agent.sha256
   ```
   C: fix mode, strip the newline, check, and delete the temporary file on the brain:
   ```powershell
   ssh vlmpi@100.122.169.70 "chmod 600 ~/edge-vision/secrets/verifiers/agent.sha256 && tr -d '\n' < ~/edge-vision/secrets/verifiers/agent.sha256 > ~/edge-vision/secrets/verifiers/agent.sha256.tmp && mv ~/edge-vision/secrets/verifiers/agent.sha256.tmp ~/edge-vision/secrets/verifiers/agent.sha256 && stat -c '%a %U %s bytes' ~/edge-vision/secrets/verifiers/agent.sha256"
   ssh homebench@100.82.120.100 "shred -u ~/.vn-verifier 2>/dev/null || rm -f ~/.vn-verifier"
   ```
   Expect `600 vlmpi 64 bytes`.
3. **sudo, once, by hand** if not done yet (user units keep running without a login):
   ```powershell
   ssh -t vlmpi@100.122.169.70 "sudo loginctl enable-linger vlmpi"
   ```
4. Re-render the units (picks up the literal llama path), then start vision-node and look at its health:
   ```powershell
   ssh vlmpi@100.122.169.70 "systemctl --user daemon-reload && systemctl --user enable --now llama-vision vision-node; sleep 8; systemctl --user --no-pager status llama-vision vision-node | head -24; curl -s http://100.122.169.70:8300/health"
   ```
   `/health` should show `"ok":true` and a single model (`qwen3-vl-2b-q4`, loaded). `ok:false` with the model loaded means
   MemAvailable is below the 400 MB guard.
5. Smoke test with a real image, from the PC (details below):
   ```powershell
   tools\vision-node-smoke.ps1 -Image C:\path\to\photo.jpg
   ```

Stop: `systemctl --user disable --now vision-node llama-vision`. Update: re-run step 1, then
`ssh vlmpi@100.122.169.70 "systemctl --user restart llama-vision vision-node"`.

## Smoke test and expected timings

`tools\vision-node-smoke.ps1` copies `tools/vision-node-smoke.py` and your image(s) to the brain, runs it there with `sudo` (the
brain sudo password; the secret is read as root from its file and never leaves the brain or appears in output), and removes the
temporary files. This also proves the exact path the agent will use: brain to `100.122.169.70:8300`. `-Via local -SecretFile F` runs it
on the PC instead if you have placed a secret file yourself. It prints health, then yes/no, short text and JSON with wall time,
the node's own `latency_ms`/`queue_ms` and the expected value, varying the question so the node's 30 s answer cache does not hide
the real time.

| Call | Expected on the vlmpi | Source |
|---|---|---|
| yes/no | about 2.3 s | benchmark `[measured]` |
| trimmed JSON (person + a short note) | about 7 s | benchmark `[measured]` |
| short sentence (40 tokens) | about 5.5 s | `[est]`, no measurement yet |

Add the network round trip (milliseconds on Tailscale) and, for the very first call after a restart, a few extra seconds while the
model pages in. A call above 3x expected is flagged SLOW. Latencies printed by the script are `[measured]` on this node, whatever
they are.

## Brain side (not done here)

`deploy/config/agent.json` needs `"sched": {"nodes": {"vision": {"enabled": true, "base_url": "http://100.122.169.70:8300"}}}`, and
`deploy/compose.yml` must mount `vision-node-api.secret` into the agent container only (it does not today); the tailnet ACL must allow
pi-brain to reach the vlmpi on 8300.
