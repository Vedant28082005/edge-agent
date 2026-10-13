<#
.SYNOPSIS
  Validates every sample in contracts/samples against contracts/announce.schema.json.
  Samples in valid/ must pass, samples in invalid/ must fail. Exit code 1 on any mismatch.
  Uses a local .venv (created on first run); nothing is installed globally.
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'Creating local .venv ...'
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
}
$ErrorActionPreference = 'Continue'   # PS 5.1 turns native stderr into errors
& $py -c "import jsonschema, yaml, openapi_spec_validator" 2>&1 | Out-Null
$haveJsonschema = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $haveJsonschema) {
    Write-Host 'Installing jsonschema, pyyaml, openapi-spec-validator into .venv ...'
    & $py -m pip install --quiet --disable-pip-version-check jsonschema pyyaml openapi-spec-validator
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}

$checker = Join-Path $PSScriptRoot 'contract_test.py'
& $py $checker (Join-Path $root 'contracts')
exit $LASTEXITCODE
