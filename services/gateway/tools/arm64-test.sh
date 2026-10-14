#!/usr/bin/env bash
# Runs INSIDE the linux/arm64 python:3.11-slim-bookworm container (see tools/gateway-arm64-test.ps1).
# Sources are mounted read-only at /src/s4 (this worktree) and /src/s1 (the device-hub worktree); everything is copied to
# /build so nothing is written to the host. Wheels only: a missing aarch64 wheel fails the install instead of compiling.
set -euo pipefail
python --version
uname -m
mkdir -p /build/edge-agent-s4/services /build/edge-agent-s1/services
tar -C /src/s4 --exclude=.venv --exclude=__pycache__ --exclude=services/gateway/data -cf - contracts services/gateway | tar -C /build/edge-agent-s4 -xf -
tar -C /src/s1 --exclude=.venv --exclude=__pycache__ -cf - contracts services/device-hub | tar -C /build/edge-agent-s1 -xf -

python -m venv /build/edge-agent-s4/services/gateway/.venv
/build/edge-agent-s4/services/gateway/.venv/bin/python -m pip install -q --only-binary=:all: -r /build/edge-agent-s4/services/gateway/requirements-dev.txt

# the real device-hub runs from its own venv, as on the PC (tests/test_integration_hub.py finds .venv/bin/python)
python -m venv /build/edge-agent-s1/services/device-hub/.venv
/build/edge-agent-s1/services/device-hub/.venv/bin/python -m pip install -q --only-binary=:all: -r /build/edge-agent-s1/services/device-hub/requirements-dev.txt

cd /build/edge-agent-s4/services/gateway
.venv/bin/python -c "import sys, sqlite3, argon2, cryptography, pydantic_core; print('py', sys.version.split()[0], 'sqlite', sqlite3.sqlite_version)"
.venv/bin/python -m pytest -q -p no:cacheprovider --timeout 300 -rs tests "$@"
