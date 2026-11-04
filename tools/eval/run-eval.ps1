<#
.SYNOPSIS
  Runs the S15 evaluation (tools\eval\phrases.json) against the live agent through the gateway, then pulls the agent's own traces
  from pi-brain and writes one CSV with the token columns.
.DESCRIPTION
  1. Flip the agent to the configuration under test first (tools\eval\configs\<label>.json is the `sched` block; edit
     /etc/edge-agent/config/agent.json on the Pi, then restart: ssh pi@pi-brain "cd ~/edge-agent/deploy && docker compose restart agent").
  2. Run this script with the same -Label. It asks for your gateway password (and a guest password if -GuestUser is given), lists any phrase
     that could raise a risk:confirm request and waits for you to type SEND, runs everything, then asks for your sudo password once to
     copy the trace DB. Output: <OutDir>\eval-<label>-<stamp>.csv (+ .json, + -traces.json).
  The harness never approves a confirmation (it declines), never touches risk:confirm devices, and prints no secret.
.EXAMPLE
  .\tools\eval\run-eval.ps1 -Label baseline -BaseUrl https://pi-brain.YOURTAILNET.ts.net -User alice -GuestUser guest
.EXAMPLE
  .\services\agent\.venv\Scripts\python.exe tools\eval\run_eval.py compare eval-results\eval-baseline-*.csv eval-results\eval-new-*.csv eval-results\eval-compact-*.csv
#>
param(
    [Parameter(Mandatory)][ValidateSet('baseline', 'new', 'compact')][string]$Label,
    [Parameter(Mandatory)][string]$BaseUrl,
    [Parameter(Mandatory)][string]$User,
    [string]$GuestUser = '',
    [string]$Target = 'pi@pi-brain',
    [string]$OutDir = 'eval-results',
    [string]$Only = '',
    [ValidateSet('ask', 'send', 'skip')][string]$ConfirmRisk = 'ask',
    [switch]$SkipTraces
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$agent = Join-Path $root 'services\agent'
$py = Join-Path $agent '.venv\Scripts\python.exe'

function Run([string]$what, [scriptblock]$cmd) {
    $ErrorActionPreference = 'Continue'      # PS 5.1 turns native stderr into errors
    & $cmd
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($code -ne 0) { throw "$what failed (exit $code)" }
}

if (-not (Test-Path $py)) {
    Write-Host 'Creating the local .venv for the agent tools (first run only) ...'
    Push-Location $agent
    try {
        Run 'venv' { py -3.12 -m venv .venv }
        Run 'pip' { .\.venv\Scripts\python -m pip install -r requirements-dev.txt }
    } finally { Pop-Location }
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $OutDir "eval-$Label-$stamp"
$runArgs = @('run', '--base-url', $BaseUrl, '--user', $User, '--label', $Label, '--out', $base, '--confirm-risk', $ConfirmRisk)
if ($GuestUser) { $runArgs += @('--guest-user', $GuestUser) }
if ($Only) { $runArgs += @('--only', $Only) }
Run 'evaluation run' { & $py (Join-Path $PSScriptRoot 'run_eval.py') @runArgs }

if (-not $SkipTraces) {
    $n = @((Get-Content "$base.json" -Raw -Encoding UTF8 | ConvertFrom-Json).rows).Count
    Run 'trace dump' { & (Join-Path $root 'tools\dump-traces.ps1') -Target $Target -Count ($n + 20) -Out "$base-traces.json" -Quiet }
    Run 'merge' { & $py (Join-Path $PSScriptRoot 'run_eval.py') merge --results "$base.json" --traces "$base-traces.json" --out "$base.csv" }
}
Run 'summary' { & $py (Join-Path $PSScriptRoot 'run_eval.py') compare "$base.csv" }
Write-Host "`nSend me: $base.csv"
