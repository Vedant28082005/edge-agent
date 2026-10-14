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
    [string]$S1 = (Join-Path (Split-Path $PSScriptRoot -Parent) '..\edge-agent-s1'),
    [string]$Log = (Join-Path $env:TEMP 'gw-arm64.log'),
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$PytestArgs
)
$ErrorActionPreference = 'Stop'
$s4 = (Resolve-Path (Split-Path $PSScriptRoot -Parent)).Path
$s1 = (Resolve-Path $S1).Path
$name = 's4-arm64-' + (-join ((48..57) + (97..102) | Get-Random -Count 4 | ForEach-Object { [char]$_ }))
Write-Host "container $name  (log: $Log)"
$cmd = 'bash /src/s4/services/gateway/tools/arm64-test.sh ' + ($PytestArgs -join ' ')
& docker run --rm --platform linux/arm64 --name $name `
    -v "${s4}:/src/s4:ro" -v "${s1}:/src/s1:ro" `
    python:3.11-slim-bookworm bash -c $cmd 2>&1 | Tee-Object -FilePath $Log
exit $LASTEXITCODE
