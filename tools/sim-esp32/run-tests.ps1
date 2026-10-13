<#
.SYNOPSIS
  Unit tests for sim-esp32 (no broker needed). Creates tools\sim-esp32\.venv on first run; nothing is installed globally.
#>
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$py = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host 'Creating local .venv ...'
    python -m venv (Join-Path $here '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
    & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $here 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}
Push-Location $here
try { & $py -m pytest -q @args; exit $LASTEXITCODE } finally { Pop-Location }
