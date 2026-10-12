#!/usr/bin/env bash
# One-time (and re-runnable) setup of broker secrets and chrony on pi-brain. Run MANUALLY on the Pi:
#
#   # from the Windows PC, stage the secret files (never part of tools\deploy.ps1):
#   ssh pi@pi-brain "mkdir -p -m 700 ~/edge-agent-setup"
#   scp deploy\mosquitto\certs\ca.crt deploy\mosquitto\certs\server.crt deploy\mosquitto\certs\server.key `
#       deploy\mosquitto\data\dynamic-security.json pi@pi-brain:edge-agent-setup/
#   # then on the Pi (interactive sudo password; no passwordless sudo needed):
#   ssh -t pi@pi-brain "bash ~/edge-agent/tools/setup-pi-brain.sh"
#
# What it does (idempotent):
#   1. checks the staged certs: key matches cert, cert chains to the CA
#   2. installs them under $STATE_DIR (default /etc/edge-agent/mosquitto), owned by uid 1883
#      (the mosquitto container user): server.key and dynamic-security.json mode 600, certs 644
#   3. writes MOSQUITTO_STATE_DIR into ~/edge-agent/deploy/.env so docker compose uses that dir
#   4. installs chrony (apt) and the edge-agent config (serve the LAN, free-run if offline)
#   5. securely deletes the staged secret files
#
# Environment overrides: STAGE_DIR, STATE_DIR, EDGE_DIR, LAN_CIDR (default 192.168.1.0/24), SKIP_CHRONY=1
set -euo pipefail

STAGE_DIR="${STAGE_DIR:-$HOME/edge-agent-setup}"
STATE_DIR="${STATE_DIR:-/etc/edge-agent/mosquitto}"
EDGE_DIR="${EDGE_DIR:-$HOME/edge-agent}"
LAN_CIDR="${LAN_CIDR:-192.168.1.0/24}"
MQ_UID=1883

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

for f in ca.crt server.crt server.key dynamic-security.json; do
  [ -f "$STAGE_DIR/$f" ] || { echo "missing $STAGE_DIR/$f (see the header of this script)" >&2; exit 1; }
done

echo "== checking certificates"
cert_pub="$(openssl x509 -in "$STAGE_DIR/server.crt" -noout -pubkey | openssl sha256)"
key_pub="$(openssl pkey -in "$STAGE_DIR/server.key" -pubout | openssl sha256)"
[ "$cert_pub" = "$key_pub" ] || { echo "server.key does not match server.crt" >&2; exit 1; }
openssl verify -CAfile "$STAGE_DIR/ca.crt" "$STAGE_DIR/server.crt" >/dev/null || { echo "server.crt does not chain to ca.crt" >&2; exit 1; }
openssl x509 -in "$STAGE_DIR/server.crt" -noout -subject -enddate -ext subjectAltName

echo "== installing into $STATE_DIR (owner uid $MQ_UID)"
$SUDO install -d -o "$MQ_UID" -g "$MQ_UID" -m 750 "$STATE_DIR" "$STATE_DIR/certs" "$STATE_DIR/data"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 644 "$STAGE_DIR/ca.crt"     "$STATE_DIR/certs/ca.crt"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 644 "$STAGE_DIR/server.crt" "$STATE_DIR/certs/server.crt"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 600 "$STAGE_DIR/server.key" "$STATE_DIR/certs/server.key"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 600 "$STAGE_DIR/dynamic-security.json" "$STATE_DIR/data/dynamic-security.json"

echo "== writing $EDGE_DIR/deploy/.env"
mkdir -p "$EDGE_DIR/deploy"
envf="$EDGE_DIR/deploy/.env"
touch "$envf"
grep -v '^MOSQUITTO_STATE_DIR=' "$envf" > "$envf.tmp" || true
echo "MOSQUITTO_STATE_DIR=$STATE_DIR" >> "$envf.tmp"
mv "$envf.tmp" "$envf"

if [ "${SKIP_CHRONY:-0}" != "1" ]; then
  echo "== chrony"
  if ! command -v chronyd >/dev/null 2>&1; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq chrony
  fi
  src="$EDGE_DIR/deploy/chrony/chrony-pi-brain.conf"
  [ -f "$src" ] || { echo "missing $src (run tools\deploy.ps1 -Target brain first)" >&2; exit 1; }
  $SUDO install -d -m 755 /etc/chrony/conf.d
  sed "s#^allow .*#allow $LAN_CIDR#" "$src" | $SUDO tee /etc/chrony/conf.d/edge-agent.conf >/dev/null
  # Debian/Raspberry Pi OS chrony reads conf.d via 'confdir'; add it if the main file lacks it.
  if ! grep -Eq '^(confdir|include)[[:space:]]+/etc/chrony/conf.d' /etc/chrony/chrony.conf 2>/dev/null; then
    echo "confdir /etc/chrony/conf.d" | $SUDO tee -a /etc/chrony/chrony.conf >/dev/null
  fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    $SUDO systemctl enable chrony >/dev/null 2>&1 || $SUDO systemctl enable chronyd >/dev/null 2>&1 || true
    $SUDO systemctl restart chrony 2>/dev/null || $SUDO systemctl restart chronyd
  else
    echo "systemd not running here; restart chrony manually" >&2
  fi
  echo "open UDP 123 from $LAN_CIDR in the firewall if one is active; verify with: chronyc tracking"
fi

echo "== removing staged secrets"
shred -u "$STAGE_DIR/server.key" "$STAGE_DIR/dynamic-security.json" 2>/dev/null || rm -f "$STAGE_DIR/server.key" "$STAGE_DIR/dynamic-security.json"
rm -f "$STAGE_DIR/ca.crt" "$STAGE_DIR/server.crt"
rmdir "$STAGE_DIR" 2>/dev/null || true
echo "done. Start the broker: cd $EDGE_DIR/deploy && docker compose up -d"
