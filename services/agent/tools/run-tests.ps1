# Runs the agent tests in the local venv (creates it on first use). Skips the real-llama test unless EDGE_TEST_GGUF is set.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements-dev.txt }
.\.venv\Scripts\python -m pytest -q @args
