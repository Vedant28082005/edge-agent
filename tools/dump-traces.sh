#!/bin/bash
# Runs ON the Pi, as root (sudo): copies the agent's sqlite files (the data dir is owned by uid 10103, mode 700), reads the copy
# with dump_traces.py, hands the result to the calling user and removes every copy. Usage: sudo bash dump-traces.sh DB N OUT
set -euo pipefail
DB="${1:-/var/lib/edge-agent/agent/agent.sqlite}"
N="${2:-15}"
OUT="${3:-/tmp/edge-traces.json}"
if [ ! -f "$DB" ]; then
  echo "no $DB; looking for it ..." >&2
  DB="$(find /var/lib /srv /opt -name agent.sqlite 2>/dev/null | head -1)"
  [ -n "$DB" ] || { echo "agent.sqlite not found (set DATA_DIR from deploy/.env: cat ~/edge-agent/deploy/.env)" >&2; exit 1; }
  echo "using $DB" >&2
fi
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
for ext in "" "-wal" "-shm"; do
  [ -e "$DB$ext" ] && cp "$DB$ext" "$WORK/agent.sqlite$ext"
done
python3 "$(dirname "$0")/dump_traces.py" "$WORK/agent.sqlite" "$OUT" "$N"
chown "${SUDO_USER:-root}" "$OUT"
