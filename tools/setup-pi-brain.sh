#!/usr/bin/env bash
# One-time (and re-runnable) setup of pi-brain: service users, secrets, data dirs, broker certs, config, chrony and the
# llama-server unit. Run MANUALLY on the Pi (never part of tools\deploy.ps1):
#
#   # from the Windows PC, stage the secret files:
#   ssh pi@pi-brain "mkdir -p -m 700 ~/edge-agent-setup"
#   scp deploy\mosquitto\certs\ca.crt deploy\mosquitto\certs\server.crt deploy\mosquitto\certs\server.key `
#       deploy\mosquitto\data\dynamic-security.json pi@pi-brain:edge-agent-setup/
#   # nothing else to stage: the hub's MQTT password is generated on the Pi (step 4b)
#   # then on the Pi (interactive sudo password; no passwordless sudo needed):
#   ssh -t pi@pi-brain "LAN_IP=192.168.1.10 LAN_CIDR=192.168.1.0/24 bash ~/edge-agent/tools/setup-pi-brain.sh"
#
# What it does (idempotent):
#   1. checks the staged certs: key matches cert, chains to the CA, and the SANs contain `mosquitto` (the name the
#      hub container uses to reach the broker; regenerate with new-certs.ps1 -ExtraDns mosquitto if not)
#   2. installs them under $STATE_DIR (default /etc/edge-agent/mosquitto), owned by uid 1883 (the mosquitto container user)
#   3. creates the service users with FIXED uids (they must match the container users): edge-device-hub 10101,
#      edge-gateway 10102, edge-agent 10103, edge-llama (llama-server, native)
#   4. creates one service-secret file per calling service, mode 600 and owned by that service's user, plus a SHA-256
#      verifier that only the CALLED service can read (a service holds only verifiers of the secrets it must check):
#        device-hub.secret -> hub calls the gateway consume endpoint ; verifier verifiers/device-hub.sha256 (gateway user)
#        gateway.secret    -> gateway calls device-hub (everything)  ; verifier hub-verifiers/gateway.sha256 (hub user)
#        agent.secret      -> agent calls device-hub (five tools)    ; verifier hub-verifiers/agent.sha256 (hub user)
#        agent-api.secret  -> gateway calls the agent's local API    ; verifier agent-verifiers/agent-api.sha256 (agent user)
#      The agent has no secret for the gateway.
#   4b. the hub's own MQTT account: hub-mqtt.password (owner hub user, mode 600) is generated on the Pi if absent
#      (a file staged as $STAGE_DIR/hub-mqtt.password is used instead, e.g. to adopt an existing password), and the
#      hub's dynsec client ($HUB_MQTT_USER, default hub-admin) with the `admin` role is created or refreshed in
#      $STATE_DIR/data/dynamic-security.json so the PBKDF2 hash always matches the file. The password is never printed
#      or put in an argument list. The broker only reads the dynsec file at start: restart mosquitto after a change.
#      The staged dynamic-security.json is installed only if the Pi has none (or REPLACE_DYNSEC=1): a re-run never
#      discards the node users the broker has since created.
#   5. creates the data dirs /var/lib/edge-agent/<service> (owned by the service uid), writes the non-secret service
#      config to /etc/edge-agent/config (device-hub.json rendered with LAN_IP) and deploy/.env for docker compose
#   6. installs the llama-server systemd unit and an env file template (never overwritten; no model is installed)
#   7. installs chrony with the edge-agent config
#   8. securely deletes the staged secret files
#
# Environment overrides: STAGE_DIR, STATE_DIR, EDGE_DIR, SECRETS_DIR, DATA_ROOT, CONFIG_DIR, SYSTEMD_DIR, LLAMA_ENV,
# LAN_IP (the Pi's static LAN IP boards connect to; required on first run), LAN_CIDR (default 192.168.1.0/24),
# SKIP_CHRONY=1, SKIP_SYSTEMD=1, ROTATE_SECRETS=1 (regenerates every secret, verifier and the hub MQTT password; restart the services and the broker after),
# REPLACE_DYNSEC=1, HUB_MQTT_USER.
set -euo pipefail

STAGE_DIR="${STAGE_DIR:-$HOME/edge-agent-setup}"
STATE_DIR="${STATE_DIR:-/etc/edge-agent/mosquitto}"
EDGE_DIR="${EDGE_DIR:-$HOME/edge-agent}"
SECRETS_DIR="${SECRETS_DIR:-/etc/edge-agent/secrets}"
DATA_ROOT="${DATA_ROOT:-/var/lib/edge-agent}"
CONFIG_DIR="${CONFIG_DIR:-/etc/edge-agent/config}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
LLAMA_ENV="${LLAMA_ENV:-/etc/edge-agent/llama-server.env}"
LAN_CIDR="${LAN_CIDR:-192.168.1.0/24}"
HUB_MQTT_USER="${HUB_MQTT_USER:-hub-admin}"
HUB_USER="${HUB_USER:-edge-device-hub}"; HUB_UID=10101
GATEWAY_USER="${GATEWAY_USER:-edge-gateway}"; GATEWAY_UID=10102
AGENT_USER="${AGENT_USER:-edge-agent}"; AGENT_UID=10103
LLAMA_USER="${LLAMA_USER:-edge-llama}"
MQ_UID=1883

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

for f in ca.crt server.crt server.key; do
  [ -f "$STAGE_DIR/$f" ] || { echo "missing $STAGE_DIR/$f (see the header of this script)" >&2; exit 1; }
done
DYNSEC="$STATE_DIR/data/dynamic-security.json"
[ -f "$STAGE_DIR/dynamic-security.json" ] || $SUDO test -f "$DYNSEC"   || { echo "missing $STAGE_DIR/dynamic-security.json (see the header of this script)" >&2; exit 1; }

echo "== checking certificates"
cert_pub="$(openssl x509 -in "$STAGE_DIR/server.crt" -noout -pubkey | openssl sha256)"
key_pub="$(openssl pkey -in "$STAGE_DIR/server.key" -pubout | openssl sha256)"
[ "$cert_pub" = "$key_pub" ] || { echo "server.key does not match server.crt" >&2; exit 1; }
openssl verify -CAfile "$STAGE_DIR/ca.crt" "$STAGE_DIR/server.crt" >/dev/null || { echo "server.crt does not chain to ca.crt" >&2; exit 1; }
openssl x509 -in "$STAGE_DIR/server.crt" -noout -subject -enddate -ext subjectAltName
openssl x509 -in "$STAGE_DIR/server.crt" -noout -ext subjectAltName | grep -q 'DNS:mosquitto' \
  || { echo "server.crt has no SAN DNS:mosquitto: the hub container could not verify the broker. Rerun new-certs.ps1 with -ExtraDns mosquitto -Force" >&2; exit 1; }

echo "== installing certs into $STATE_DIR (owner uid $MQ_UID)"
$SUDO install -d -o "$MQ_UID" -g "$MQ_UID" -m 750 "$STATE_DIR" "$STATE_DIR/certs" "$STATE_DIR/data"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 644 "$STAGE_DIR/ca.crt"     "$STATE_DIR/certs/ca.crt"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 644 "$STAGE_DIR/server.crt" "$STATE_DIR/certs/server.crt"
$SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 600 "$STAGE_DIR/server.key" "$STATE_DIR/certs/server.key"
if [ -f "$STAGE_DIR/dynamic-security.json" ] && { ! $SUDO test -f "$DYNSEC" || [ "${REPLACE_DYNSEC:-0}" = "1" ]; }; then
  $SUDO install -o "$MQ_UID" -g "$MQ_UID" -m 600 "$STAGE_DIR/dynamic-security.json" "$DYNSEC"
  echo "installed dynamic-security.json"
else
  echo "keeping the existing dynamic-security.json (REPLACE_DYNSEC=1 replaces it with the staged one)"
fi

echo "== service users (fixed uids)"
ensure_user() {  # $1 name, $2 uid ('' = any)
  if id -u "$1" >/dev/null 2>&1; then
    if [ -n "$2" ] && [ "$(id -u "$1")" != "$2" ]; then echo "user $1 exists with uid $(id -u "$1"), the container needs $2" >&2; exit 1; fi
  else
    $SUDO groupadd --system ${2:+--gid "$2"} "$1" 2>/dev/null || true
    $SUDO useradd --system ${2:+--uid "$2"} --gid "$1" --no-create-home --shell /usr/sbin/nologin "$1"
  fi
}
ensure_user "$HUB_USER" "$HUB_UID"
ensure_user "$GATEWAY_USER" "$GATEWAY_UID"
ensure_user "$AGENT_USER" "$AGENT_UID"
ensure_user "$LLAMA_USER" ""

echo "== service secrets ($SECRETS_DIR)"
# Directories are traverse-only (711): names are not listable, files are protected by their own mode/owner.
$SUDO install -d -o root -g root -m 711 "$SECRETS_DIR"
$SUDO install -d -o "$GATEWAY_USER" -g "$GATEWAY_USER" -m 700 "$SECRETS_DIR/verifiers"        # read by the gateway
$SUDO install -d -o "$HUB_USER"     -g "$HUB_USER"     -m 700 "$SECRETS_DIR/hub-verifiers"    # read by device-hub
$SUDO install -d -o "$AGENT_USER"   -g "$AGENT_USER"   -m 700 "$SECRETS_DIR/agent-verifiers"  # read by the agent
make_secret() {  # $1 = secret name, $2 = owning user (the caller), $3 = verifier dir name, $4 = verifier owner (the verifying service's user)
  local f="$SECRETS_DIR/$1.secret" v="$SECRETS_DIR/$3/$1.sha256"
  if [ "${ROTATE_SECRETS:-0}" = "1" ] || ! $SUDO test -f "$f" || ! $SUDO test -f "$v"; then
    openssl rand -base64 32 | tr -d '\n' | $SUDO tee "$f" >/dev/null
    $SUDO chown "$2:$2" "$f"; $SUDO chmod 600 "$f"
    # verifier for the called service: SHA-256 of the secret; it can verify but cannot impersonate the caller
    $SUDO cat "$f" | openssl dgst -sha256 -r | cut -d' ' -f1 | $SUDO tee "$v" >/dev/null
    $SUDO chown "$4:$4" "$v"; $SUDO chmod 600 "$v"
    echo "created $f (owner $2, mode 600) and verifier $v (owner $4, mode 600)"
  else
    echo "keeping existing $f"
  fi
}
make_secret device-hub "$HUB_USER"     verifiers       "$GATEWAY_USER"
make_secret gateway    "$GATEWAY_USER" hub-verifiers   "$HUB_USER"
make_secret agent      "$AGENT_USER"   hub-verifiers   "$HUB_USER"
make_secret agent-api  "$GATEWAY_USER" agent-verifiers "$AGENT_USER"
# Deliberately no agent secret for the gateway: the agent cannot mint or consume confirmation tokens.
echo "== hub MQTT account ($HUB_MQTT_USER)"
HUB_PW="$SECRETS_DIR/hub-mqtt.password"
if [ -f "$STAGE_DIR/hub-mqtt.password" ]; then
  [ -n "$(tr -d '\r\n' < "$STAGE_DIR/hub-mqtt.password")" ] || { echo "staged hub-mqtt.password is empty" >&2; exit 1; }
  tr -d '\r\n' < "$STAGE_DIR/hub-mqtt.password" | $SUDO tee "$HUB_PW" >/dev/null
  hub_pw_state="installed from the staged file"
elif [ "${ROTATE_SECRETS:-0}" = "1" ] || ! $SUDO test -s "$HUB_PW"; then
  openssl rand -base64 33 | tr -d '\n=+/' | $SUDO tee "$HUB_PW" >/dev/null
  hub_pw_state="generated"
else
  hub_pw_state="kept"
fi
$SUDO chown "$HUB_USER:$HUB_USER" "$HUB_PW"; $SUDO chmod 600 "$HUB_PW"
echo "$HUB_PW: $hub_pw_state (owner $HUB_USER, mode 600)"
command -v python3 >/dev/null 2>&1 || { $SUDO apt-get update -qq; $SUDO apt-get install -y -qq python3 >/dev/null; }
# Create/refresh the hub's dynsec client + role so the stored PBKDF2 hash matches the file. The password is read from
# the file by this process (never argv/env/stdout); the dynsec file is rewritten atomically as uid $MQ_UID, mode 600.
$SUDO python3 - "$DYNSEC" "$HUB_PW" "$HUB_MQTT_USER" "$MQ_UID" <<'PYEOF'
import base64, hashlib, json, os, sys
path, pwfile, user, uid = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
pw = open(pwfile, "rb").read()
doc = json.load(open(path))
roles = doc.setdefault("roles", [])
role = next((r for r in roles if r.get("rolename") == "admin"), None)
if role is None:
    acls = [("publishClientSend", "$CONTROL/dynamic-security/#"), ("publishClientReceive", "$CONTROL/dynamic-security/#"),
            ("publishClientReceive", "$SYS/#"), ("publishClientReceive", "#"), ("publishClientSend", "#"),
            ("subscribePattern", "$CONTROL/dynamic-security/#"), ("subscribePattern", "$SYS/#"),
            ("subscribePattern", "#"), ("unsubscribePattern", "#")]
    roles.append({"rolename": "admin", "textname": "", "acls": [{"acltype": t, "topic": p, "allow": True} for t, p in acls]})
clients = doc.setdefault("clients", [])
cl = next((c for c in clients if c.get("username") == user), None)
if cl is None:
    cl = {"username": user, "textName": "", "roles": []}
    clients.append(cl)
changed = role is None or "salt" not in cl
if not any(r.get("rolename") == "admin" for r in cl.setdefault("roles", [])):
    cl["roles"].append({"rolename": "admin"}); changed = True
iters = int(cl.get("iterations") or 101)
def derive(salt): return base64.b64encode(hashlib.pbkdf2_hmac("sha512", pw, salt, iters)).decode()
if not changed and cl.get("password") == derive(base64.b64decode(cl["salt"])):
    print("dynsec client %s already matches the password file" % user); sys.exit(0)
salt = os.urandom(12)
cl["salt"], cl["iterations"], cl["password"] = base64.b64encode(salt).decode(), iters, derive(salt)
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(doc, f, indent=2)
os.chown(tmp, uid, uid); os.chmod(tmp, 0o600); os.replace(tmp, path)
print("dynsec client %s created/refreshed with the admin role: restart mosquitto to load it" % user)
PYEOF

echo "== data dirs ($DATA_ROOT)"
$SUDO install -d -o root -g root -m 755 "$DATA_ROOT"
$SUDO install -d -o "$HUB_UID"     -g "$HUB_UID"     -m 750 "$DATA_ROOT/device-hub"
$SUDO install -d -o "$GATEWAY_UID" -g "$GATEWAY_UID" -m 750 "$DATA_ROOT/gateway"
$SUDO install -d -o "$AGENT_USER"  -g "$AGENT_USER"  -m 750 "$DATA_ROOT/agent"
$SUDO install -d -o root -g "$LLAMA_USER" -m 750 "$DATA_ROOT/models"   # put the GGUF here (not installed by this script)

echo "== service config ($CONFIG_DIR, no secrets)"
$SUDO install -d -o root -g root -m 755 "$CONFIG_DIR"
for f in gateway.json agent.json; do
  [ -f "$EDGE_DIR/deploy/config/$f" ] || { echo "missing $EDGE_DIR/deploy/config/$f (run tools\\deploy.ps1 -Target brain first)" >&2; exit 1; }
  $SUDO install -o root -g root -m 644 "$EDGE_DIR/deploy/config/$f" "$CONFIG_DIR/$f"
done
if [ -n "${LAN_IP:-}" ]; then
  case "$LAN_IP" in *[!0-9.]*|"") echo "LAN_IP must be an IPv4 address" >&2; exit 1;; esac
  sed "s#__BROKER_HOST__#$LAN_IP#g" "$EDGE_DIR/deploy/config/device-hub.json.template" | $SUDO tee "$CONFIG_DIR/device-hub.json" >/dev/null
  $SUDO chmod 644 "$CONFIG_DIR/device-hub.json"
elif ! $SUDO test -f "$CONFIG_DIR/device-hub.json"; then
  echo "LAN_IP is required on the first run (the Pi's static LAN address that boards connect to)" >&2; exit 1
fi

echo "== writing $EDGE_DIR/deploy/.env"
mkdir -p "$EDGE_DIR/deploy"
envf="$EDGE_DIR/deploy/.env"
touch "$envf"
grep -v -E '^(MOSQUITTO_STATE_DIR|SECRETS_DIR|DATA_ROOT|DATA_DIR|CONFIG_DIR)=' "$envf" > "$envf.tmp" || true
{ echo "MOSQUITTO_STATE_DIR=$STATE_DIR"; echo "SECRETS_DIR=$SECRETS_DIR"; echo "DATA_DIR=$DATA_ROOT"; echo "CONFIG_DIR=$CONFIG_DIR"; } >> "$envf.tmp"
mv "$envf.tmp" "$envf"

if [ "${SKIP_SYSTEMD:-0}" != "1" ]; then
  echo "== llama-server unit"
  [ -f "$EDGE_DIR/deploy/systemd/llama-server.service" ] || { echo "missing deploy/systemd/llama-server.service" >&2; exit 1; }
  $SUDO install -d -m 755 "$SYSTEMD_DIR" "$(dirname "$LLAMA_ENV")"
  $SUDO install -o root -g root -m 644 "$EDGE_DIR/deploy/systemd/llama-server.service" "$SYSTEMD_DIR/llama-server.service"
  if ! $SUDO test -f "$LLAMA_ENV"; then   # never overwrite an edited env file
    $SUDO install -o root -g "$LLAMA_USER" -m 640 "$EDGE_DIR/deploy/systemd/llama-server.env.template" "$LLAMA_ENV"
    echo "created $LLAMA_ENV: set LLAMA_MODEL to your GGUF, then: sudo systemctl enable --now llama-server"
  fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable llama-server >/dev/null 2>&1 || true
    model="$($SUDO sed -n 's/^LLAMA_MODEL=//p' "$LLAMA_ENV" | tail -1)"
    if [ -n "$model" ] && $SUDO test -r "$model" && $SUDO test -x /usr/local/bin/llama-server; then
      $SUDO systemctl restart llama-server && echo "llama-server restarted"
    else
      echo "llama-server enabled but NOT started: set LLAMA_MODEL in $LLAMA_ENV and install /usr/local/bin/llama-server"
    fi
  else
    echo "systemd not running here; enable llama-server manually" >&2
  fi
fi

if [ "${SKIP_CHRONY:-0}" != "1" ]; then
  echo "== chrony"
  if ! command -v chronyd >/dev/null 2>&1; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq chrony
  fi
  src="$EDGE_DIR/deploy/chrony/chrony-pi-brain.conf"
  [ -f "$src" ] || { echo "missing $src (run tools\\deploy.ps1 -Target brain first)" >&2; exit 1; }
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
shred -u "$STAGE_DIR/server.key" "$STAGE_DIR/dynamic-security.json" "$STAGE_DIR/hub-mqtt.password" 2>/dev/null \
  || rm -f "$STAGE_DIR/server.key" "$STAGE_DIR/dynamic-security.json" "$STAGE_DIR/hub-mqtt.password"
rm -f "$STAGE_DIR/ca.crt" "$STAGE_DIR/server.crt"
rmdir "$STAGE_DIR" 2>/dev/null || true
echo "done. Restart the broker if the dynsec file changed. Load the images and start the stack: tools\\deploy.ps1 -Target brain -Up (then: tools/tailscale-serve.md)"
echo "first admin (once): docker compose -f $EDGE_DIR/deploy/compose.yml run --rm -it gateway --config /etc/edge-agent/config/gateway.json bootstrap-admin --username NAME"
