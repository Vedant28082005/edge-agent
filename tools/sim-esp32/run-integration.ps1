<#
.SYNOPSIS
  Integration run of sim-esp32 against the dev broker (deploy\compose.dev.yml).
  Prereqs (deploy\README.md): Docker running, deploy\mosquitto\new-certs.ps1 + dynsec-bootstrap.ps1 done,
  `docker compose -f compose.yml -f compose.dev.yml up -d` running in deploy\.
.DESCRIPTION
  Creates three throwaway node users with deploy\mosquitto\dynsec-node-user.ps1 (a DEV-PC helper: it puts the
  hub password on a docker exec command line, which is why services must never use it), exports only paths and
  node ids to the tests (never a password), runs the integration tests, and removes the users again.
  Credentials live in deploy\mosquitto\certs\nodes\ (git-ignored).
#>
param([string]$Container = 'edge-mosquitto')
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$root = Split-Path -Parent (Split-Path -Parent $here)
$mq = Join-Path $root 'deploy\mosquitto'

docker version --format '{{.Server.Version}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host 'Docker is not available; integration run skipped.'; exit 3 }
$running = docker ps --filter "name=^$Container$" --format '{{.Names}}'
if (-not $running) { Write-Host "Container $Container is not running; start the dev broker first (deploy\README.md)."; exit 3 }
foreach ($f in 'certs\ca.crt', '.env') {
    if (-not (Test-Path (Join-Path $mq $f))) { Write-Host "Missing deploy\mosquitto\$f; run new-certs.ps1 and dynsec-bootstrap.ps1."; exit 3 }
}

$py = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { & (Join-Path $here 'run-tests.ps1') | Out-Null }

$run = -join ((1..8) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })   # fresh ids per run: no stale retained topics
$ids = 1..3 | ForEach-Object { "esp32-5a$run{0:x2}" -f $_ }
$code = 1
try {
    # dynsec-node-user.ps1 is not idempotent (an existing user keeps its old password): clear leftovers first
    foreach ($id in $ids) { try { & (Join-Path $mq 'dynsec-node-user.ps1') -NodeId $id -Container $Container -Remove | Out-Null } catch { } }
    foreach ($id in $ids) { & (Join-Path $mq 'dynsec-node-user.ps1') -NodeId $id -Container $Container | Out-Null }
    $env:SIM_IT_NODES = $ids -join ','
    $env:SIM_MQTT_CA = Join-Path $mq 'certs\ca.crt'
    $env:SIM_MQTT_CREDS_DIR = Join-Path $mq 'certs\nodes'
    $env:SIM_IT_HUB_ENV = Join-Path $mq '.env'
    Push-Location $here
    try {
        $ErrorActionPreference = 'Continue'
        & $py -m pytest -m integration tests\test_integration.py -v -p no:cacheprovider
        $code = $LASTEXITCODE
    } finally { Pop-Location; $ErrorActionPreference = 'Stop' }
}
finally {
    foreach ($id in $ids) {
        try { & (Join-Path $mq 'dynsec-node-user.ps1') -NodeId $id -Container $Container -Remove | Out-Null } catch { }
        Remove-Item (Join-Path $mq "certs\nodes\$id.cred") -ErrorAction SilentlyContinue
    }
    'SIM_IT_NODES', 'SIM_MQTT_CA', 'SIM_MQTT_CREDS_DIR', 'SIM_IT_HUB_ENV' | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
}
exit $code
