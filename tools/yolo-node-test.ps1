<#
.SYNOPSIS
  Runs the yolo-node tests (mock backend, no GPU, no camera, no network): contract conformance of every response
  against contracts/openapi/yolo-node-internal.yaml, auth, size/format limits, queue full, timeout, no-bytes-in-logs,
  ONNX backend on a synthetic model, TensorRT worker protocol with a stub worker, and a real --mock server process.
  Uses the repo's local .venv (created on first run); nothing is installed globally.
.PARAMETER PytestArgs
  Extra arguments for pytest, e.g. -k queue
#>
param([string[]]$PytestArgs = @())
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host 'Creating local .venv ...'
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
}
$req = Join-Path $root 'services\yolo-node\requirements-dev.txt'
$ErrorActionPreference = 'Continue'   # PS 5.1 turns native stderr into errors
& $py -c "import starlette, uvicorn, numpy, PIL, httpx, jsonschema, yaml, pytest, pytest_asyncio, onnx, onnxruntime" 2>&1 | Out-Null
$have = ($LASTEXITCODE -eq 0)
if (-not $have) {
    Write-Host 'Installing yolo-node test dependencies into .venv ...'
    & $py -m pip install --quiet --disable-pip-version-check -r $req onnx 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}
Push-Location (Join-Path $root 'services\yolo-node')
try {
    & $py -m pytest -q @PytestArgs 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
} finally { Pop-Location }
exit $code
