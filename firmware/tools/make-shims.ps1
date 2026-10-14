<#
.SYNOPSIS
  Creates gcc/g++/ar/ranlib shims (in firmware\.shim, git-ignored) that call the zig C++ compiler from the
  local .venv, for dev PCs without a host C++ toolchain. `pio test -e native` needs a host compiler.
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$py = Join-Path $root '.venv\Scripts\python.exe'
$shim = Join-Path (Split-Path -Parent $PSScriptRoot) '.shim'
if (-not (Test-Path $py)) { throw ".venv missing: run firmware\tools\test.ps1 (it creates it)" }
New-Item -ItemType Directory -Force $shim | Out-Null
$map = @{ 'gcc' = 'cc'; 'g++' = 'c++'; 'ar' = 'ar'; 'ranlib' = 'ranlib' }
foreach ($k in $map.Keys) {
    $line = "@`"$py`" -m ziglang $($map[$k]) %*"
    Set-Content -Path (Join-Path $shim "$k.cmd") -Value @('@echo off', $line) -Encoding ASCII
}
Write-Output $shim
