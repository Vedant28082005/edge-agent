<#
.SYNOPSIS
  Tests services/vision-node on the dev PC: unit + conformance tests (mock backends, schemas taken from
  contracts/openapi/vision-node-internal.yaml), then a real-socket smoke run of `python -m vision_node --mock`.
  With -Image it also builds the arm64 image and runs the mock smoke inside it (needs Docker Desktop).
.PARAMETER Image
  Build deploy/docker/Dockerfile.vision-node for linux/arm64 (emulated), start ONE throwaway container with a unique
  name (read-only fs, uid 10104, mock mode), wait for its HEALTHCHECK, remove only that container.
.PARAMETER PytestArgs
  Extra pytest arguments, e.g. -k queue.
.NOTES
  Uses the repo-local .venv (created on first run); nothing is installed globally. Starts no broker and no model server;
  the smoke test stops only the child process it launched.
#>
param(
    [switch]$Image,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$PytestArgs
)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$root = Split-Path -Parent $here
$svc  = Join-Path $root 'services\vision-node'
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'Creating local .venv ...'
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
}
$ErrorActionPreference = 'Continue'   # PS 5.1 turns native stderr into errors
& $py -c "import starlette, uvicorn, httpx, jsonschema, yaml, PIL, numpy, pytest, pytest_timeout" 2>&1 | Out-Null
$have = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $have) {
    Write-Host 'Installing vision-node dev requirements into .venv ...'
    & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $svc 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}

$env:PYTHONPATH = $svc
$env:PYTHONDONTWRITEBYTECODE = '1'
$code = 0
Push-Location $svc
try {
    $ErrorActionPreference = 'Continue'
    & $py -m pytest -q -p no:cacheprovider @PytestArgs
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        & $py (Join-Path $svc 'tools\smoke.py')
        $code = $LASTEXITCODE
    }
    $ErrorActionPreference = 'Stop'
} finally { Pop-Location }
if ($code -ne 0) { exit $code }

if ($Image) {
    $name = 's6-vn-' + (-join ((48..57) + (97..102) | Get-Random -Count 6 | ForEach-Object { [char]$_ }))
    $tag  = 'edge-agent/vision-node:s6test-arm64'
    $ErrorActionPreference = 'Continue'
    & docker buildx build --platform linux/arm64 --load -f (Join-Path $root 'deploy\docker\Dockerfile.vision-node') -t $tag $root
    if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL image build' -ForegroundColor Red; exit 1 }
    & docker run -d --name $name --platform linux/arm64 --read-only --tmpfs /tmp --user 10104:10104 --cap-drop ALL `
        --security-opt no-new-privileges:true --memory 256m $tag --mock --insecure-no-auth | Out-Null
    $status = 'starting'
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline -and $status -eq 'starting') {
        Start-Sleep -Seconds 5
        $status = (docker inspect -f '{{.State.Health.Status}}' $name 2>$null)
        if ((docker inspect -f '{{.State.Running}}' $name 2>$null) -ne 'true') { $status = 'exited' }
    }
    if ($status -ne 'healthy') { docker logs --tail 25 $name 2>&1 | ForEach-Object { Write-Host "  $_" } }
    docker rm -f $name | Out-Null          # only the container started here
    if ($status -eq 'healthy') { Write-Host 'PASS image (mock, arm64 emulated): healthy' -ForegroundColor Green }
    else { Write-Host "FAIL image: $status" -ForegroundColor Red; exit 1 }
}
Write-Host 'vision-node tests passed.'
