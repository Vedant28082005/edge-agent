# vision-node on the vlmpi, native (no Docker, VLM only)

Runs the existing `services/vision-node` (contract `vision-node-internal.yaml` 1.0.0, unchanged) as a venv + two systemd **user**
units on the vlmpi (Pi 5, 4 GB, Debian 13, Python 3.13): `llama-vision` (llama-server, Qwen3-VL-2B Q4_K_M + mmproj Q8_0,
`-c 1024 -np 1 -t 4`, 127.0.0.1:8081) and `vision-node` (listens on the Tailscale address, port 8300, `X-Service-Secret`).
Whisper and YOLO are switched off (`"whisper": {"enabled": false}`, `"yolo": ...`): their operations answer 503
`vision_unavailable` and `/health` reports only the VLM. Images are downscaled to 224 px (`limits.vlm_max_side`) before llama-server.
Memory limits (`MemoryMax` 3000M / 300M) are `[est]`. Nothing here is verified on the real Pi yet.

## Steps (from the PC, one at a time; only step 4 needs the vlmpi sudo password)

1. Ship the code and run the no-sudo installer (starts nothing, sends no secret):
   ```powershell
   tools\vlmpi-deploy.ps1
   ```
   It prints "config + contract OK" at the end. `-StageOnly` shows what would be sent without touching the vlmpi.
2. Check the llama.env paths the installer wrote (model names match `~/pi_bench/models`):
   ```powershell
   ssh vlmpi@100.122.169.70 "cat ~/edge-vision/config/llama.env; ls ~/pi_bench/llama.cpp/build/bin/llama-server"
   ```
3. Create the agent's secret **on pi-brain** and write only its verifier to the vlmpi. The verifier is the SHA-256 of the secret
   text WITHOUT the trailing newline (the agent strips it before sending; `sha256sum file` would hash the newline and never match):
   ```powershell
   ssh -t pi@pi-brain "sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 | tr -d \"\n\" > /etc/edge-agent/secrets/vision-node-api.secret'; sudo tr -d '\n' < /etc/edge-agent/secrets/vision-node-api.secret | sha256sum | cut -d' ' -f1"
   ```
   Then put the printed 64 hex characters on the vlmpi:
   ```powershell
   ssh vlmpi@100.122.169.70 "printf '%s' '<64 hex>' > ~/edge-vision/secrets/verifiers/agent.sha256 && chmod 600 ~/edge-vision/secrets/verifiers/agent.sha256"
   ```
4. **sudo, once, by hand** (keeps user units running without a login):
   ```powershell
   ssh -t vlmpi@100.122.169.70 "sudo loginctl enable-linger vlmpi"
   ```
5. Start and look:
   ```powershell
   ssh vlmpi@100.122.169.70 "systemctl --user enable --now llama-vision vision-node; sleep 20; systemctl --user --no-pager status llama-vision vision-node | head -30; curl -s http://100.122.169.70:8300/health"
   ```
   `/health` should show `"ok": true` once llama-server has loaded the model (about a minute).

Update later: re-run step 1 and `ssh ... "systemctl --user restart llama-vision vision-node"`. Stop:
`systemctl --user disable --now vision-node llama-vision`.

## Brain side (not done here)

`deploy/config/agent.json` needs `"sched": {"nodes": {"vision": {"enabled": true, "base_url": "http://100.122.169.70:8300"}}}`, and
`deploy/compose.yml` must mount `vision-node-api.secret` into the agent container (it does not today); the tailnet ACL must allow
pi-brain to reach the vlmpi on 8300.
