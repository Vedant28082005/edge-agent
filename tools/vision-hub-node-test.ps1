<#
.SYNOPSIS
  Tests services/vision-hub-node on the dev PC (fake llama-server, no model, no broker, no Docker).
#>
param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$PytestArgs)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$root = Split-Path -Parent $here
$svc  = Join-Path $root 'services\vision-hub-node'
$py   = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    python -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
}
$ErrorActionPreference = 'Continue'
& $py -c "import starlette, uvicorn, httpx, PIL, pytest, pytest_timeout" 2>&1 | Out-Null
$have = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $have) {
    & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $svc 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}
Push-Location $svc
try {
    $ErrorActionPreference = 'Continue'
    & $py -m pytest -q @PytestArgs
    $rc = $LASTEXITCODE
} finally { Pop-Location }
exit $rc
