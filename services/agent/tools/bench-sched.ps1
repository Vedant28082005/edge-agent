# Offline scheduler benchmark (mock LLM, simulated latencies). Creates the local venv on first use. Usage: .\tools\bench-sched.ps1 [-Json out.json]
param([string]$Json = '')
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$ErrorActionPreference = 'Continue'      # native stderr must not become a terminating error (CLAUDE.md, PS 5.1 rules)
if (-not (Test-Path .venv)) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
    .\.venv\Scripts\python -m pip install -q -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}
$argv = @('tools\bench_sched.py')
if ($Json) { $argv += @('--json', $Json) }
.\.venv\Scripts\python @argv | Out-String | Write-Host
if ($LASTEXITCODE -ne 0) { throw "bench failed ($LASTEXITCODE)" }
