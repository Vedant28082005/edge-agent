<#
.SYNOPSIS
  Provisions and starts several simulated nodes in one go (one hidden background python per node).

.DESCRIPTION
  Node i (1..Count) uses profile Profiles[(i-1) % Profiles.Count] and the label sim-<profile>-NN, NN counting per
  profile (sim-light-01, sim-fan-01, ...). The admin password is asked ONCE (hidden) and handed to provision-sim.ps1 in
  memory only. Each node is provisioned and started straight away, well inside the 10 minute session window.
  PIDs go to .sim\room.pids.json (git-ignored); logs to .sim\<node_id>\sim.log. Stop with stop-room.ps1.

.EXAMPLE
  .\start-room.ps1 -GatewayUrl https://<gateway> -Count 4
  .\start-room.ps1 -GatewayUrl https://<gateway> -Count 3 -Profiles room,relay4,sensors
#>
param(
    [Parameter(Mandatory)] [string] $GatewayUrl,
    [string] $AdminUser = 'admin',
    [int] $Count = 4,
    [string[]] $Profiles = @('light', 'fan', 'curtain', 'dht'),
    [string] $SimHost = '',
    [string] $WifiProfile = ''
)
$ErrorActionPreference = 'Stop'
$valid = 'light', 'fan', 'curtain', 'dht', 'relay4', 'sensors', 'room'
foreach ($p in $Profiles) { if ($p -notin $valid) { throw "Unknown profile '$p' (valid: $($valid -join ', '))" } }
if ($Count -lt 1 -or $Count -gt 32) { throw 'Count must be 1..32' }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'No .venv yet: run .\run-tests.ps1 once first' }
$simDir = Join-Path $PSScriptRoot '.sim'
$pidFile = Join-Path $simDir 'room.pids.json'
New-Item -ItemType Directory -Force -Path $simDir | Out-Null
if (Test-Path $pidFile) { throw "$pidFile exists: run .\stop-room.ps1 first" }

$secure = Read-Host -Prompt "Admin password for '$AdminUser' (asked once)" -AsSecureString
$seen = @{}; $started = @()
try {
    for ($i = 0; $i -lt $Count; $i++) {
        $prof = $Profiles[$i % $Profiles.Count]
        $seen[$prof] = 1 + [int]$seen[$prof]
        $label = 'sim-{0}-{1:d2}' -f $prof, $seen[$prof]
        $pa = @{ NodeId = $label; GatewayUrl = $GatewayUrl; AdminUser = $AdminUser; AdminPassword = $secure; SimDir = $simDir; PassThru = $true }
        if ($WifiProfile) { $pa.WifiProfile = $WifiProfile }
        $out = & (Join-Path $PSScriptRoot 'provision-sim.ps1') @pa
        $nid = @($out | Where-Object { $_ -is [string] -and $_ -match '^esp32-[0-9a-f]{12}$' })[-1]
        if (-not $nid) { throw "Could not read the node id for $label" }
        $simArgs = @('-u', (Join-Path $PSScriptRoot 'sim_node.py'), '--profile', $prof, '--node-id', $nid, '--sim-dir', $simDir)
        if ($SimHost) { $simArgs += @('--host', $SimHost) }
        $log = Join-Path $simDir "$nid\sim.log"
        $proc = Start-Process -FilePath $py -ArgumentList $simArgs -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        $started += [pscustomobject]@{ pid = $proc.Id; node_id = $nid; label = $label; profile = $prof; log = $log }
        Write-Host ("started {0,-18} {1}  pid {2}" -f $label, $nid, $proc.Id)
    }
} finally {
    $secure = $null
    if ($started) { ConvertTo-Json -InputObject @($started) | Out-File -Encoding utf8 $pidFile }
}
Write-Host ''
Write-Host 'Node ids:'; $started | ForEach-Object { Write-Host ("  {0}  ({1}, {2})" -f $_.node_id, $_.label, $_.profile) }
Write-Host ''
Write-Host 'REMINDER: approve each one in the dashboard (Admin > pending node > name and room).'
Write-Host 'Logs: .sim\<node_id>\sim.log   Stop all: .\stop-room.ps1'
