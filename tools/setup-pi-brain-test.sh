#!/usr/bin/env bash
# Runs INSIDE a throwaway debian-bookworm container (see setup-pi-brain-test.ps1): exercises tools/setup-pi-brain.sh as
# root with fake staged certs, twice, and checks users/uids, ownership/modes, verifiers, idempotency and rotation.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
command -v openssl >/dev/null || { apt-get update -qq && apt-get install -y -qq openssl >/dev/null; }
fail=0; ok() { echo "PASS $*"; }; bad() { echo "FAIL $*"; fail=1; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

W=/tmp/w; rm -rf $W; mkdir -p $W/stage $W/edge/deploy $W/edge/tools
cp -r /src/deploy/config /src/deploy/systemd /src/deploy/chrony $W/edge/deploy/
export STAGE_DIR=$W/stage EDGE_DIR=$W/edge STATE_DIR=$W/etc/mosquitto SECRETS_DIR=$W/etc/secrets DATA_ROOT=$W/var \
  CONFIG_DIR=$W/etc/config SYSTEMD_DIR=$W/etc/systemd LLAMA_ENV=$W/etc/llama-server.env SKIP_CHRONY=1 LAN_IP=192.168.1.10
stage() {
  mkdir -p $W/stage; cd $W/stage
  openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -subj /CN=test-ca -days 2 >/dev/null 2>&1
  openssl req -newkey rsa:2048 -nodes -keyout server.key -out s.csr -subj /CN=pi-brain >/dev/null 2>&1
  printf 'subjectAltName=DNS:localhost,DNS:mosquitto,IP:192.168.1.10\n' > ext
  openssl x509 -req -in s.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 2 -extfile ext >/dev/null 2>&1
  rm -f ca.key s.csr ext ca.srl; echo '{}' > dynamic-security.json; printf 'hubpw-test' > hub-mqtt.password
  cd /
}
stage
bash /src/tools/setup-pi-brain.sh >$W/run1.log 2>&1 || { cat $W/run1.log; bad "first run"; exit 1; }
ok "first run"
S=$SECRETS_DIR
own() { stat -c '%U:%a' "$1"; }
chk "uids fixed"            '[ "$(id -u edge-device-hub)" = 10101 ] && [ "$(id -u edge-gateway)" = 10102 ] && [ "$(id -u edge-agent)" = 10103 ]'
chk "device-hub.secret owned by hub, 600"   '[ "$(own $S/device-hub.secret)" = edge-device-hub:600 ]'
chk "gateway.secret owned by gateway, 600"  '[ "$(own $S/gateway.secret)" = edge-gateway:600 ]'
chk "agent.secret owned by agent, 600"      '[ "$(own $S/agent.secret)" = edge-agent:600 ]'
chk "agent-api.secret owned by gateway, 600" '[ "$(own $S/agent-api.secret)" = edge-gateway:600 ]'
chk "verifier of device-hub read by gateway" '[ "$(own $S/verifiers/device-hub.sha256)" = edge-gateway:600 ]'
chk "hub verifiers (gateway, agent) owned by hub" '[ "$(own $S/hub-verifiers/gateway.sha256)" = edge-device-hub:600 ] && [ "$(own $S/hub-verifiers/agent.sha256)" = edge-device-hub:600 ]'
chk "agent-api verifier owned by agent"     '[ "$(own $S/agent-verifiers/agent-api.sha256)" = edge-agent:600 ]'
chk "hub-mqtt.password owned by hub, 600"   '[ "$(own $S/hub-mqtt.password)" = edge-device-hub:600 ]'
chk "no agent-to-gateway secret exists"     '! ls $S | grep -q -E "agent-gateway|agent-to-gateway"'
for p in "device-hub verifiers/device-hub" "gateway hub-verifiers/gateway" "agent hub-verifiers/agent" "agent-api agent-verifiers/agent-api"; do
  sec=${p% *}; ver=${p#* }
  chk "verifier = sha256(secret) for $sec" '[ "$(cat $S/$sec.secret | openssl dgst -sha256 -r | cut -d" " -f1)" = "$(cat $S/$ver.sha256)" ]'
done
chk "secret has no newline (what services send)" '[ "$(wc -l < $S/gateway.secret)" = 0 ] && [ "$(wc -c < $S/gateway.secret)" -ge 40 ]'
chk "data dirs owned by service uids"  '[ "$(stat -c %u $DATA_ROOT/device-hub)" = 10101 ] && [ "$(stat -c %u $DATA_ROOT/gateway)" = 10102 ] && [ "$(stat -c %u $DATA_ROOT/agent)" = 10103 ]'
chk "device-hub.json rendered with LAN_IP"  'grep -q "\"broker_host\": \"192.168.1.10\"" $CONFIG_DIR/device-hub.json && ! grep -q __BROKER_HOST__ $CONFIG_DIR/device-hub.json'
chk "service config has no secret value"    '! grep -rq "$(cat $S/gateway.secret)" $CONFIG_DIR'
chk "llama unit + env template installed, env never a model" '[ -f $SYSTEMD_DIR/llama-server.service ] && grep -q "CHANGE-ME" $LLAMA_ENV'
chk "deploy/.env points at dirs"            'grep -q "^SECRETS_DIR=$S" $EDGE_DIR/deploy/.env && grep -q "^DATA_DIR=$DATA_ROOT" $EDGE_DIR/deploy/.env'
chk "staged secrets removed"                '[ ! -e $W/stage/server.key ] && [ ! -e $W/stage/hub-mqtt.password ]'
h1="$(cat $S/gateway.secret $S/agent.secret $S/agent-api.secret $S/device-hub.secret | sha256sum)"
echo "edited" > $LLAMA_ENV                  # an edited env file must survive a re-run
stage; rm $W/stage/hub-mqtt.password                                # password only needed on the first run
bash /src/tools/setup-pi-brain.sh >$W/run2.log 2>&1 || { cat $W/run2.log; bad "second run"; exit 1; }
chk "second run keeps every secret"         '[ "$h1" = "$(cat $S/gateway.secret $S/agent.secret $S/agent-api.secret $S/device-hub.secret | sha256sum)" ]'
chk "edited llama env not overwritten"      '[ "$(cat $LLAMA_ENV)" = edited ]'
stage; ROTATE_SECRETS=1 bash /src/tools/setup-pi-brain.sh >$W/run3.log 2>&1
chk "ROTATE_SECRETS=1 regenerates"          '[ "$h1" != "$(cat $S/gateway.secret $S/agent.secret $S/agent-api.secret $S/device-hub.secret | sha256sum)" ]'
chk "verifier follows the rotated secret"   '[ "$(cat $S/gateway.secret | openssl dgst -sha256 -r | cut -d" " -f1)" = "$(cat $S/hub-verifiers/gateway.sha256)" ]'
# a cert without the `mosquitto` SAN is refused
stage
mkdir -p $W/stage; cd $W/stage && printf 'subjectAltName=DNS:localhost\n' > ext2 && openssl req -newkey rsa:2048 -nodes -keyout server.key -out s2.csr -subj /CN=x >/dev/null 2>&1 \
  && openssl req -x509 -newkey rsa:2048 -nodes -keyout ck -out ca.crt -subj /CN=ca2 -days 2 >/dev/null 2>&1 \
  && openssl x509 -req -in s2.csr -CA ca.crt -CAkey ck -CAcreateserial -out server.crt -days 2 -extfile ext2 >/dev/null 2>&1; cd /
if bash /src/tools/setup-pi-brain.sh >$W/run4.log 2>&1; then bad "cert without SAN mosquitto accepted"; else grep -q "DNS:mosquitto" $W/run4.log && ok "cert without SAN mosquitto refused"; fi
exit $fail
