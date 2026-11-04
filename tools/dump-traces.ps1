<#
.SYNOPSIS
  Dumps the agent's last N turn traces (+ the user's text and the reply) from pi-brain into a local JSON file.
.DESCRIPTION
  The agent's sqlite lives on the Pi at /var/lib/edge-agent/agent/agent.sqlite (compose mounts ${DATA_DIR}/agent at
  /var/lib/edge-agent in the container; agent.json db_path = /var/lib/edge-agent/agent.sqlite). The directory is owned by the
  agent's uid (10103), so the copy needs sudo: you are asked for the sudo password once (ssh -t). Only a COPY is read; the live
  database is untouched, and every temporary file on the Pi is removed. No secret is read or printed.
.PARAMETER Count  how many traces (default 15)
.PARAMETER Target ssh target (default pi@pi-brain, see tools\nodes.psd1)
.PARAMETER Db     path of agent.sqlite on the Pi if DATA_DIR is not /var/lib/edge-agent
.PARAMETER Out    local output file (default traces.json in the current folder)
.EXAMPLE
  .\tools\dump-traces.ps1
#>
param(
    [int]$Count = 15,
    [string]$Target = 'pi@pi-brain',
    [string]$Db = '/var/lib/edge-agent/agent/agent.sqlite',
    [string]$Out = 'traces.json'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$remote = '/tmp/edge-traces.json'

function Run([string]$what, [scriptblock]$cmd) {
    $ErrorActionPreference = 'Continue'      # PS 5.1 turns native stderr into errors
    & $cmd
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($code -ne 0) { throw "$what failed (exit $code)" }
}

Run 'scp scripts' { scp (Join-Path $here 'dump_traces.py') (Join-Path $here 'dump-traces.sh') "${Target}:/tmp/" }
Run 'remote dump' { ssh -t $Target "sudo bash /tmp/dump-traces.sh $Db $Count $remote" }
Run 'scp result' { scp "${Target}:$remote" $Out }
Run 'cleanup' { ssh $Target "rm -f /tmp/dump_traces.py /tmp/dump-traces.sh $remote" }

$rows = Get-Content $Out -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($r in $rows) {
    $steps = ($r.steps | ForEach-Object { "$($_.step)[eval=$($_.prompt_n) cached=$($_.cache_n) gen=$($_.predicted_n) $($_.ms)ms]" }) -join ' '
    "{0} #{1} '{2}' -> {3}/{4} {5}ms text_source={6} say_verdict={7} roster={8}`n    steps: {9}`n    llm_raw: {10}`n    reply: {11}" -f `
        $r.time, $r.id, $r.user_text, $r.route, $r.decision, $r.latency_ms, $r.text_source, $r.say_verdict, $r.roster_hash, $steps, $r.llm_raw, $r.reply
}
"`nFull detail written to $((Resolve-Path $Out).Path) - send that file."
