#!/usr/bin/env bash
# Run ON the vlmpi as the vlmpi user. NO sudo. Installs the vision-node natively (venv + systemd USER units).
# Starts nothing. Safe to re-run, also after a partial run: every step checks before it acts, and an existing
# verifier/secret or config is never overwritten.
#   bash ~/edge-vision/stage/deploy/vlmpi/install.sh <tailscale-ip>
# Keep apostrophes out of ${VAR:?message} words: bash reads them as an opening quote (that was the first version's bug).
set -euo pipefail

BIND="${1:?usage: install.sh TAILSCALE_IP (the address this node listens on, e.g. 100.x.y.z)}"
case "$BIND" in
  0.0.0.0|::|"") echo "refusing a wildcard bind"; exit 2 ;;
esac
case "$BIND" in
  *[!0-9a-fA-F.:]*) echo "bad address: $BIND"; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$(cd "$HERE/../.." && pwd)"
BASE="$HOME/edge-vision"
UNITS="$HOME/.config/systemd/user"

python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || { echo "need Python >= 3.11"; exit 1; }

umask 077
mkdir -p "$BASE/app" "$BASE/config" "$BASE/secrets/verifiers"
chmod 700 "$BASE/secrets" "$BASE/secrets/verifiers"

# code: the service package and the one contract file it validates against (replaced as a whole, so a partial copy heals)
rm -rf "$BASE/app/vision_node" "$BASE/app/contracts"
cp -r "$STAGE/services/vision-node/vision_node" "$BASE/app/vision_node"
mkdir -p "$BASE/app/contracts/openapi"
cp "$STAGE/contracts/openapi/vision-node-internal.yaml" "$BASE/app/contracts/openapi/"
find "$BASE/app" -name __pycache__ -prune -exec rm -rf {} +

# venv, wheels only (fails loudly if a pinned wheel does not exist for aarch64 / this Python). Re-creates a half-made venv.
if [ ! -x "$BASE/venv/bin/python" ] || ! "$BASE/venv/bin/python" -c 'import sys' 2>/dev/null; then
  rm -rf "$BASE/venv"
  python3 -m venv "$BASE/venv"
fi
"$BASE/venv/bin/python" -m pip install --quiet --disable-pip-version-check --only-binary=:all: -r "$HERE/requirements-vlmpi.txt"

# config (non-secret). An existing file is KEPT; the new rendering goes next to it when it differs, for you to diff.
render() { sed -e "s|__HOME__|$HOME|g" -e "s|__BIND__|$BIND|g" "$1"; }
render "$HERE/vision-node.json.template" > "$BASE/config/vision-node.json.new"
if [ -f "$BASE/config/vision-node.json" ]; then
  if cmp -s "$BASE/config/vision-node.json" "$BASE/config/vision-node.json.new"; then
    rm -f "$BASE/config/vision-node.json.new"
  else
    echo "kept existing config/vision-node.json (new rendering: config/vision-node.json.new)"
  fi
else
  mv "$BASE/config/vision-node.json.new" "$BASE/config/vision-node.json"
fi
if [ ! -f "$BASE/config/llama.env" ]; then
  render "$HERE/llama.env.template" > "$BASE/config/llama.env"
fi

# units (code, not configuration: refreshed on every run); nothing is enabled or started.
# systemd does not expand ${VAR} in the first word of ExecStart, so the llama-server path is rendered in from llama.env.
VLM_BIN="$(sed -n -e 's/^VLM_BIN=//p' "$BASE/config/llama.env" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/')"
case "$VLM_BIN" in
  /*) ;;
  *) echo "llama.env: VLM_BIN must be an absolute path"; exit 1 ;;
esac
case "$VLM_BIN" in
  *[!A-Za-z0-9_./+-]*) echo "llama.env: VLM_BIN has characters that are not allowed in a unit path"; exit 1 ;;
esac
[ -x "$VLM_BIN" ] || { echo "llama.env: VLM_BIN is not an executable file: $VLM_BIN"; exit 1; }
mkdir -p "$UNITS"
sed -e "s|__VLM_BIN__|$VLM_BIN|" "$HERE/llama-vision.service.template" > "$UNITS/llama-vision.service"
cp "$HERE/vision-node.service" "$UNITS/"
systemctl --user daemon-reload || echo "note: systemctl --user is not available in this shell; run daemon-reload in a login session"

# offline self-check: the node must load its config and contract
( cd "$BASE/app" && PYTHONPATH="$BASE/app" "$BASE/venv/bin/python" - <<'PY'
import os
from vision_node.config import load_config
from vision_node.contract import Contract
c = load_config(os.path.expanduser("~/edge-vision/config/vision-node.json")).validate(mock=False)
Contract(c.contracts_dir)
print("config + contract OK:", c.host, c.port, "whisper", c.whisper_enabled, "yolo", c.yolo_enabled, "vlm_max_side", c.vlm_max_side)
PY
)

echo
if [ -s "$BASE/secrets/verifiers/agent.sha256" ]; then
  echo "verifier present (not touched)"
else
  echo "TODO verifier: write the 64 hex characters (sha256 of the agent secret, no trailing newline) to"
  echo "     $BASE/secrets/verifiers/agent.sha256 and chmod 600 it"
fi
echo "TODO once, by hand with sudo:  sudo loginctl enable-linger $(id -un)"
echo "Then:  systemctl --user enable --now llama-vision vision-node      (nothing has been started)"
