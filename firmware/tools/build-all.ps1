<#
.SYNOPSIS
  Builds every example for esp32dev (`pio run`). Never uploads: there is no upload step here and no
  upload_port in platformio.ini.
#>
$ErrorActionPreference = 'Stop'
$fw   = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $fw
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$pio  = Join-Path $venv 'Scripts\pio.exe'
if (-not (Test-Path $pio)) {
    if (-not (Test-Path $py)) { python -m venv $venv; if ($LASTEXITCODE -ne 0) { throw 'venv failed' } }
    & $py -m pip install --quiet --disable-pip-version-check platformio
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}
Push-Location $fw
try {
    $ErrorActionPreference = 'Continue'
    & $pio run
    exit $LASTEXITCODE
} finally { Pop-Location }
