# deploy/config

Non-secret service configuration, mounted read-only at `/etc/edge-agent/config` (host dir `CONFIG_DIR`, default
`/etc/edge-agent/config`). `gateway.json` and `agent.json` are used as they are; `device-hub.json.template` has one
placeholder, `__BROKER_HOST__` (the Pi's static LAN IP that boards connect to), rendered into `device-hub.json` by
`tools/setup-pi-brain.sh` (`LAN_IP=...`) and by `tools\e2e-smoke.ps1`. Secrets are only ever file paths here.

Binding `0.0.0.0` is for the container's private network only: compose does not publish 8100, 8200 or 8001.
The agent's `llm_url` reaches the native llama-server through `host.docker.internal` (compose maps it to the host gateway).
