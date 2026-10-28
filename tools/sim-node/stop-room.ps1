<#
.SYNOPSIS
  Stops the sims started by start-room.ps1. Only the PIDs recorded there, and only if the PID is still a python
  process running sim_node.py with the recorded node id (never by process name).
#>
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot '.sim\room.pids.json'
if (-not (Test-Path $pidFile)) { Write-Host 'Nothing to stop (no .sim\room.pids.json).'; exit 0 }
$rows = @(Get-Content -Raw $pidFile | ConvertFrom-Json)
foreach ($r in $rows) {
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($r.pid)" -ErrorAction SilentlyContinue
    if ($cim -and $cim.CommandLine -match 'sim_node\.py' -and $cim.CommandLine -match [regex]::Escape($r.node_id)) {
        Stop-Process -Id $r.pid -Force   # hard stop: the broker fires the LWT, node goes offline
        Write-Host "stopped $($r.label) ($($r.node_id)) pid $($r.pid)"
    } else { Write-Host "skip $($r.label): pid $($r.pid) is not that sim any more" }
}
Remove-Item $pidFile
Write-Host 'Done. Forget the nodes in the dashboard and delete .sim\<node_id> when finished.'
