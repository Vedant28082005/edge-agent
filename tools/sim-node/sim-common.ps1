# Dot-sourced by start-sims.ps1 / stop-sims.ps1 / provision-sim.ps1: finds (or creates) the local .venv python.
function Get-SimPython {
    $py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        if (-not (Test-Path $py)) {
            Write-Host 'Creating local .venv ...'
            python -m venv (Join-Path $PSScriptRoot '.venv')
            if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed (is Python 3.10+ on PATH?)' }
        }
        & $py -c 'import psutil, paho, jsonschema, yaml' 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Installing requirements into .venv ...'
            & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
            if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
        }
    } finally { $ErrorActionPreference = $prev }
    return $py
}
