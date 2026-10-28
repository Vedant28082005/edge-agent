<#
.SYNOPSIS
  Stops the sims started by start-sims / start-room: only recorded PIDs that are still this tool's sim_node.py,
  plus leftover sim_node.py processes of this tool. Never stops anything by image name.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'sim-common.ps1')
$py = Get-SimPython
& $py (Join-Path $PSScriptRoot 'simctl.py') stop
exit $LASTEXITCODE
