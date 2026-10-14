<#
.SYNOPSIS
  Static policy test for deploy\compose.yml (no containers are started): the file is valid, only the gateway
  (127.0.0.1:8000) and mosquitto (8883) publish ports, and no service mounts another service's secret files.
#>
param([string]$ComposeFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\compose.yml'))
$ErrorActionPreference = 'Stop'
$fail = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS $name" } else { Write-Host "FAIL $name $detail" -ForegroundColor Red; $script:fail++ }
}

# Fixed, fake host paths so every mount source is predictable.
$env:SECRETS_DIR = '/h/secrets'; $env:DATA_DIR = '/h/data'; $env:CONFIG_DIR = '/h/config'; $env:MOSQUITTO_STATE_DIR = '/h/mq'
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$json = docker compose -f $ComposeFile config --format json 2>&1
$code = $LASTEXITCODE; $ErrorActionPreference = $prev
Check 'docker compose config is valid' ($code -eq 0) ($json -join ' ')
if ($code -ne 0) { exit 1 }
$cfg = ($json -join "`n") | ConvertFrom-Json
$svc = $cfg.services
$names = @($svc.PSObject.Properties.Name)
Check 'services are mosquitto, device-hub, gateway, agent' ((($names | Sort-Object) -join ',') -eq 'agent,device-hub,gateway,mosquitto') ($names -join ',')

# --- published ports -------------------------------------------------------------------------------------------------
foreach ($n in $names) {
    $ports = @($svc.$n.ports)
    if ($n -eq 'gateway') {
        Check 'gateway publishes exactly 127.0.0.1:8000' ($ports.Count -eq 1 -and $ports[0].target -eq 8000 -and "$($ports[0].published)" -eq '8000' -and $ports[0].host_ip -eq '127.0.0.1') ($ports | ConvertTo-Json -Compress)
    } elseif ($n -eq 'mosquitto') {
        Check 'mosquitto publishes exactly 8883' ($ports.Count -eq 1 -and $ports[0].target -eq 8883 -and "$($ports[0].published)" -eq '8883') ($ports | ConvertTo-Json -Compress)
    } else {
        Check "$n publishes no port" ($ports.Count -eq 0 -or $null -eq $ports[0])
    }
    Check "$n does not use host networking" ($svc.$n.network_mode -ne 'host')
}
$allTargets = @($names | ForEach-Object { @($svc.$_.ports) | Where-Object { $_ } | ForEach-Object { $_.target } })
Check '8100, 8200 and 8001 are not published' (-not ($allTargets | Where-Object { $_ -in 8100, 8200, 8001 }))
Check 'exactly one network' (@($cfg.networks.PSObject.Properties.Name).Count -eq 1)

# --- secrets: which service may mount which file ---------------------------------------------------------------------
$allowed = @{
    'device-hub' = @('/h/secrets/device-hub.secret', '/h/secrets/hub-mqtt.password', '/h/secrets/hub-verifiers')
    'gateway'    = @('/h/secrets/gateway.secret', '/h/secrets/agent-api.secret', '/h/secrets/verifiers')
    'agent'      = @('/h/secrets/agent.secret', '/h/secrets/agent-verifiers')
    'mosquitto'  = @()
}
$secretNames = 'device-hub.secret', 'hub-mqtt.password', 'hub-verifiers', 'gateway.secret', 'agent-api.secret', 'verifiers', 'agent.secret', 'agent-verifiers'  # scan-ok: test fixture, not a credential
foreach ($n in $names) {
    $src = @($svc.$n.volumes | ForEach-Object { $_.source.Replace('\', '/') })
    $mine = @($src | Where-Object { $_ -like '/h/secrets*' })
    $foreign = @($mine | Where-Object { $_ -notin $allowed[$n] })
    Check "$n mounts only its own secret files" ($foreign.Count -eq 0) ("foreign: " + ($foreign -join ', '))
    Check "$n mounts exactly its allowed set" ((($mine | Sort-Object) -join ',') -eq (($allowed[$n] | Sort-Object) -join ',')) ($mine -join ',')
    # nothing broader than a single file or verifier dir (e.g. the whole secrets dir)
    Check "$n does not mount the secrets root" ('/h/secrets' -notin $src)
    Check "$n has no CA private key or other service's data dir" (-not ($src | Where-Object { $_ -like '*ca.key*' -or ($_ -like '/h/data/*' -and $_ -ne "/h/data/$n") }))
    $envs = if ($svc.$n.environment) { @($svc.$n.environment.PSObject.Properties.Name) } else { @() }
    Check "$n has no secret-looking environment variable" (-not ($envs | Where-Object { $_ -match 'SECRET|PASSWORD|TOKEN|KEY' }))
}
Check 'device-hub has no agent-api secret/verifier' (-not (@($svc.'device-hub'.volumes.source) -match 'agent-api|agent-verifiers'))
Check 'agent has no gateway secret' (-not (@($svc.agent.volumes.source) -match 'gateway\.secret|device-hub\.secret|agent-api\.secret|/verifiers|hub-verifiers'))
Check 'gateway cannot read the hub MQTT password or agent secret' (-not (@($svc.gateway.volumes.source) -match 'hub-mqtt|agent\.secret|hub-verifiers'))

# --- hardening / operations ------------------------------------------------------------------------------------------
$uids = @{}
foreach ($n in 'device-hub', 'gateway', 'agent') {
    $s = $svc.$n
    Check "$n runs as a fixed non-root uid" ($s.user -match '^[1-9][0-9]{4,}:[1-9][0-9]{4,}$') $s.user
    $uids[$n] = $s.user
    Check "$n has a read-only root fs and a tmpfs" ($s.read_only -eq $true -and @($s.tmpfs).Count -ge 1)
    $dataMounts = @($s.volumes | Where-Object { $_.target -eq '/var/lib/edge-agent' })
    $writable = @($s.volumes | Where-Object { -not $_.read_only })
    Check "$n has exactly one writable volume (its data dir)" ($dataMounts.Count -eq 1 -and $writable.Count -eq 1 -and $dataMounts[0].source -eq "/h/data/$n") (($writable.source) -join ',')
    Check "$n has a mem_limit" ($s.mem_limit -gt 0)
    Check "$n drops all capabilities, no-new-privileges" ((@($s.cap_drop) -contains 'ALL') -and (@($s.security_opt) -contains 'no-new-privileges:true'))
}
Check 'uids are distinct' ((@($uids.Values | Sort-Object -Unique)).Count -eq 3)
foreach ($n in $names) {
    Check "$n restarts unless-stopped" ($svc.$n.restart -eq 'unless-stopped')
    $o = $svc.$n.logging.options
    Check "$n has json-file log rotation" ($svc.$n.logging.driver -eq 'json-file' -and $o.'max-size' -and $o.'max-file')
}
Write-Host ''
if ($fail -gt 0) { Write-Host "$fail check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'compose policy test passed' -ForegroundColor Green
