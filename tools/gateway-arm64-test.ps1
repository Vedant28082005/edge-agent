<#
  Runs the gateway test suite (including the unix-socket test and the real-hub integration) on Python 3.11 in an
  EMULATED linux/arm64 Debian bookworm container, installing with pip --only-binary=:all: (no compiling).

  Usage (PowerShell 5.1, repo worktree edge-agent-s4; the device-hub worktree must sit next to it as ..\edge-agent-s1):
      .\tools\gateway-arm64-test.ps1 [-S1 ..\edge-agent-s1] [-Log C:\temp\gw-arm64.log] [pytest args...]

  Starts ONE container with a unique name (s4-arm64-<random>) and removes only that container (--rm). Sources are mounted
  read-only; nothing is written to the repo. Needs Docker Desktop with arm64 emulation (check: docker run --rm --platform
  linux/arm64 alpine uname -m  ->  aarch64).
#>
param(
    [string]$S1,
    [string]$Log = (Join-Path $env:TEMP 'gw-arm64.log'),
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$PytestArgs
)
$ErrorActionPreference = 'Stop'
# $PSScriptRoot is not reliable inside param() defaults on Windows PowerShell 5.1, so derive paths here
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$s4 = (Resolve-Path (Split-Path $here -Parent)).Path
if (-not $S1) { $S1 = Join-Path (Split-Path $s4 -Parent) 'edge-agent-s1' }
$s1 = (Resolve-Path $S1).Path
$name = 's4-arm64-' + (-join ((48..57) + (97..102) | Get-Random -Count 4 | ForEach-Object { [char]$_ }))
Write-Host "container $name  (log: $Log)"
$cmd = 'bash /src/s4/services/gateway/tools/arm64-test.sh ' + ($PytestArgs -join ' ')
Set-Content -LiteralPath $Log -Value '' -Encoding UTF8
# Windows PowerShell 5.1 turns a native command's stderr (2>&1) into terminating errors under 'Stop', which would kill the
# pipeline and the --rm container mid-run: relax it for the docker call only, and keep pip quiet.
$ErrorActionPreference = 'Continue'
& docker run --rm --platform linux/arm64 --name $name `
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PYTHONUNBUFFERED=1 `
    -v "${s4}:/src/s4:ro" -v "${s1}:/src/s1:ro" `
    python:3.11-slim-bookworm bash -c $cmd 2>&1 |
    ForEach-Object { $line = "$_"; Write-Host $line; Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 }
exit $LASTEXITCODE
