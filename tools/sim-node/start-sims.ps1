<#
.SYNOPSIS
  Stops any running sims (no duplicates), then starts every saved simulated node hidden in the background and prints
  a status table after about 10 seconds. No provisioning, no password: it uses .sim\<node_id>\ as it is.
  Called by start-sims.bat. `-Mode auto` is used by the login shortcut (same behaviour, no pause in the .bat).
#>
param([string] $Mode = '', [string] $BrokerHost = '', [double] $Wait = 10)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'sim-common.ps1')
$py = Get-SimPython
$a = @((Join-Path $PSScriptRoot 'simctl.py'), 'start', '--wait', $Wait)
if ($BrokerHost) { $a += @('--host', $BrokerHost) }
& $py @a
exit $LASTEXITCODE
