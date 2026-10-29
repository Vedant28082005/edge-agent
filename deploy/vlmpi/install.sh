#!/usr/bin/env bash
# Run ON the vlmpi as the vlmpi user. NO sudo. Installs the vision-node natively (venv + systemd USER units). Starts nothing.
#   bash ~/edge-vision/stage/deploy/vlmpi/install.sh <tailscale-ip>
set -euo pipefail
BIND="${1:?usage: install.sh <this node's tailscale ip, e.g. 100.x.y.z>}"
case "$BIND" in 0.0.0.0|::|"") echo "refusing a wildcard bind"; exit 2;; esac
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$(cd "$HERE/../.." && pwd)"                     # ~/edge-vision/stage
BASE="$HOME/edge-vision"
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || { echo "need Python >= 3.11"; exit 1; }

umask 077
mkdir -p "$BASE/app" "$BASE/config" "$BASE/secrets/verifiers"
chmod 700 "$BASE/secrets" "$BASE/secrets/verifiers"

# code: the service package and the one contract file it validates against
rm -rf "$BASE/app/vision_node" "$BASE/app/contracts"
cp -r "$STAGE/services/vision-node/vision_node" "$BASE/app/vision_node"
mkdir -p "$BASE/app/contracts/openapi"
cp "$STAGE/contracts/openapi/vision-node-internal.yaml" "$BASE/app/contracts/openapi/"
find "$BASE/app" -name __pycache__ -prune -exec rm -rf {} +

# venv, wheels only (fails loudly if a pinned wheel does not exist for aarch64 / this Python)
[ -x "$BASE/venv/bin/python" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install --quiet --disable-pip-version-check --only-binary=:all: -r "$HERE/requirements-vlmpi.txt"

# config (non-secret). llama.env is kept if it already exists so local edits survive a re-install.
sed -e "s|__HOME__|$HOME|g" -e "s|__BIND__|$BIND|g" "$HERE/vision-node.json.template" > "$BASE/config/vision-node.json"
[ -f "$BASE/config/llama.env" ] || sed -e "s|__HOME__|$HOME|g" "$HERE/llama.env.template" > "$BASE/config/llama.env"

mkdir -p "$HOME/.config/systemd/user"
cp "$HERE/llama-vision.service" "$HERE/vision-node.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload || echo "note: systemctl --user unavailable in this shell; run daemon-reload from a login session"

# offline self-check: the node must load its config and contract (mock mode, loopback, no auth, then stopped)
( cd "$BASE/app" && PYTHONPATH="$BASE/app" "$BASE/venv/bin/python" - <<'PY'
from vision_node.config import load_config
import os
c = load_config(os.path.expanduser("~/edge-vision/config/vision-node.json")).validate(mock=False)
from vision_node.contract import Contract
Contract(c.contracts_dir)
print("config + contract OK:", c.host, c.port, "whisper", c.whisper_enabled, "yolo", c.yolo_enabled, "vlm_max_side", c.vlm_max_side)
PY
)
echo
echo "Installed. Still to do (nothing is started):"
echo "  1. verifier:  printf '%s' '<sha256 hex of the agent secret>' > $BASE/secrets/verifiers/agent.sha256 && chmod 600 it"
echo "  2. once, by hand with sudo:  sudo loginctl enable-linger $USER"
echo "  3. systemctl --user enable --now llama-vision vision-node"
