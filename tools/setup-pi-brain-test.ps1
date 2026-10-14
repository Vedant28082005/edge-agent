<#
.SYNOPSIS
  Tests tools/setup-pi-brain.sh in a throwaway Debian bookworm container (root inside, fake certs, nothing touches the
  host or any real Pi): users/uids, secret ownership and modes, verifiers, idempotency, rotation, SAN check.
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$name = 'setup-test-' + (-join ((1..6) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) }))
$ErrorActionPreference = 'Continue'
docker run --rm --name $name -v "${root}:/src:ro" debian:bookworm-slim bash /src/tools/setup-pi-brain-test.sh
$code = $LASTEXITCODE
if ($code -ne 0) { Write-Host "setup-pi-brain test FAILED (exit $code)" -ForegroundColor Red } else { Write-Host 'setup-pi-brain test passed' -ForegroundColor Green }
exit $code
