<#
.SYNOPSIS
  Proves node isolation on a running dev broker: node A can publish to its own topic, is denied on node B's.
  Prereqs (see deploy/README.md): Docker running, new-certs.ps1, dynsec-bootstrap.ps1, compose up.
  Creates two throwaway nodes, runs the checks, removes them. Exit 0 = all assertions held.
.NOTES
  STATUS: run against the dev broker (Mosquitto 2.0.20, Docker Desktop on Windows); see the commit message for the result.
#>
param([string]$Container = 'edge-mosquitto')
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$e = @{}; Get-Content (Join-Path $here '.env') | ForEach-Object { $k, $v = $_ -split '=', 2; $e[$k] = $v }
$a = 'esp32-aaaaaaaaaaaa'; $b = 'esp32-bbbbbbbbbbbb'

function Get-Cred($id) {
    $c = @{}; Get-Content (Join-Path $here "certs\nodes\$id.cred") | ForEach-Object { $k, $v = $_ -split '=', 2; $c[$k] = $v }; $c
}
function Mq([string]$tool, [string[]]$a2) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = docker exec $Container $tool -h localhost -p 8883 --cafile /mosquitto/certs/ca.crt -V 5 @a2 2>&1
    $code = $LASTEXITCODE; $ErrorActionPreference = $prev
    [pscustomobject]@{ Code = $code; Out = ($out -join "`n") }
}
$fail = 0
function Assert([bool]$cond, [string]$msg) { if ($cond) { Write-Host "PASS $msg" } else { Write-Host "FAIL $msg"; $script:fail++ } }

try {
    foreach ($id in $a, $b) { & (Join-Path $here 'dynsec-node-user.ps1') -NodeId $id -Container $Container | Out-Null }
    $ca = Get-Cred $a; $cb = Get-Cred $b

    # hub listens to both nodes' state topics
    $sub = Start-Job -ScriptBlock {
        param($c, $u, $p)
        docker exec $c mosquitto_sub -h localhost -p 8883 --cafile /mosquitto/certs/ca.crt -u $u -P $p -t 'home/v1/nodes/+/state' -W 8 -C 2 -v 2>&1
    } -ArgumentList $Container, $e['HUB_ADMIN_USER'], $e['HUB_ADMIN_PASSWORD']
    Start-Sleep 2

    $own = Mq 'mosquitto_pub' @('-u', $ca.username, '-P', $ca.password, '-i', $a, '-t', "home/v1/nodes/$a/state", '-q', '1', '-m', '{"from":"A-own"}')
    Assert ($own.Code -eq 0) "node A publishes to its own topic (exit $($own.Code))"

    $other = Mq 'mosquitto_pub' @('-u', $ca.username, '-P', $ca.password, '-i', $a, '-t', "home/v1/nodes/$b/state", '-q', '1', '-m', '{"from":"A-on-B"}')
    Assert ($other.Code -ne 0 -or $other.Out -match 'not authori[sz]ed') "node A is denied on node B's topic (exit $($other.Code): $($other.Out.Trim()))"

    $seen = (Receive-Job $sub -Wait -AutoRemoveJob) -join "`n"
    Assert ($seen -match 'A-own') 'hub saw the message A sent to its own topic'
    Assert ($seen -notmatch 'A-on-B') "hub never saw A's message on B's topic"

    $subOther = Mq 'mosquitto_sub' @('-u', $ca.username, '-P', $ca.password, '-i', $a, '-t', "home/v1/nodes/$b/#", '-W', '2', '-C', '1')
    Assert ($subOther.Out -match 'not authori[sz]ed|denied' -or $subOther.Code -ne 0) "node A cannot subscribe to B's subtree ($($subOther.Out.Trim()))"

    $subOwn = Mq 'mosquitto_sub' @('-u', $ca.username, '-P', $ca.password, '-i', $a, '-t', "home/v1/nodes/$a/cmd", '-W', '2', '-C', '1')
    Assert ($subOwn.Out -notmatch 'denied|not authori[sz]ed') "node A can subscribe to its own cmd topic ($($subOwn.Out.Trim()))"

    # hub -> node: the hub admin can publish a cmd and node A receives it
    $nodeSub = Start-Job -ScriptBlock {
        param($c, $u, $p, $id)
        docker exec $c mosquitto_sub -h localhost -p 8883 --cafile /mosquitto/certs/ca.crt -u $u -P $p -i $id -t "home/v1/nodes/$id/cmd" -W 6 -C 1 2>&1
    } -ArgumentList $Container, $ca.username, $ca.password, $a
    Start-Sleep 2
    Mq 'mosquitto_pub' @('-u', $e['HUB_ADMIN_USER'], '-P', $e['HUB_ADMIN_PASSWORD'], '-i', 'hub-test', '-t', "home/v1/nodes/$a/cmd", '-q', '1', '-m', 'hub-cmd-ok') | Out-Null
    $got = (Receive-Job $nodeSub -Wait -AutoRemoveJob) -join "`n"
    Assert ($got -match 'hub-cmd-ok') 'hub publishes a cmd and node A receives it'

    $anon = Mq 'mosquitto_pub' @('-i', 'anon', '-t', "home/v1/nodes/$a/state", '-m', 'x')
    Assert ($anon.Code -ne 0) 'anonymous connection is refused'

    # revocation: remove A, then it must not connect
    & (Join-Path $here 'dynsec-node-user.ps1') -NodeId $a -Container $Container -Remove | Out-Null
    $gone = Mq 'mosquitto_pub' @('-u', $ca.username, '-P', $ca.password, '-i', $a, '-t', "home/v1/nodes/$a/state", '-m', 'x')
    Assert ($gone.Code -ne 0) 'revoked node A can no longer connect'
}
finally {
    foreach ($id in $a, $b) { try { & (Join-Path $here 'dynsec-node-user.ps1') -NodeId $id -Container $Container -Remove | Out-Null } catch { } }
    Remove-Item (Join-Path $here 'certs\nodes\esp32-aaaaaaaaaaaa.cred'), (Join-Path $here 'certs\nodes\esp32-bbbbbbbbbbbb.cred') -ErrorAction SilentlyContinue
}
if ($fail) { Write-Host "$fail assertion(s) failed"; exit 1 }
Write-Host 'All ACL assertions passed.'
