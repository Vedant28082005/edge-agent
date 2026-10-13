<#
.SYNOPSIS
  Host-side tests for the firmware core: PlatformIO native unit tests, then validation of what every
  example would publish (announce, state, cmd, ack) against contracts/*.schema.json.
  Needs no board. Python packages go into the repo-local .venv only.
#>
$ErrorActionPreference = 'Stop'
$fw   = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $fw
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$pio  = Join-Path $venv 'Scripts\pio.exe'

if (-not (Test-Path $py)) { Write-Host 'Creating local .venv ...'; python -m venv $venv; if ($LASTEXITCODE -ne 0) { throw 'venv failed' } }
$ErrorActionPreference = 'Continue'
& $py -c "import platformio, jsonschema, ziglang" 2>&1 | Out-Null
$have = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $have) {
    Write-Host 'Installing platformio, jsonschema, ziglang into .venv ...'
    & $py -m pip install --quiet --disable-pip-version-check platformio jsonschema pyyaml ziglang
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}

# `pio test -e native` needs a host g++. If there is none, use the zig compiler from the .venv through shims.
if (-not (Get-Command g++ -ErrorAction SilentlyContinue)) {
    $shim = & (Join-Path $PSScriptRoot 'make-shims.ps1')
    $env:PATH = "$shim;$env:PATH"
    Write-Host "No host g++ found: using zig shims in $shim"
}

$out = Join-Path $fw '.pio\contract-out'
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null
$env:EDGE_CONTRACT_OUT = $out

Push-Location $fw
try {
    $ErrorActionPreference = 'Continue'
    & $pio test -e native 2>&1 | Where-Object { $_ -notmatch 'warning:|note:|_Nullable|_Nonnull|^\s*\d+ \||^\s+\||Compiling|Archiving|Indexing' }
    $unit = $LASTEXITCODE
    Write-Host ''
    Write-Host '--- contract validation of example output ---'
    & $py -I (Join-Path $PSScriptRoot 'check_contracts.py') $out (Join-Path $root 'contracts')
    $contract = $LASTEXITCODE
    Write-Host ''
    Write-Host '--- build-time clock floor (2026-10-01) ---'
    & $py -I (Join-Path $PSScriptRoot 'test_build_floor.py')
    if ($LASTEXITCODE -ne 0) { $contract = 1 }
} finally { Pop-Location }

if ($unit -ne 0 -or $contract -ne 0) { Write-Host "FAILED (unit tests: $unit, contract check: $contract)"; exit 1 }
Write-Host 'ALL PASSED'
