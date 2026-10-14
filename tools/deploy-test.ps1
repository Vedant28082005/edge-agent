<#
.SYNOPSIS
  Dry-run test of tools\deploy.ps1: no ssh/scp is ever executed (both are shadowed by functions that fail the test if
  called), the brain plan contains the expected steps, and nothing secret can be in the copy list.
#>
$ErrorActionPreference = 'Stop'
$script:fail = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host "PASS $name" } else { Write-Host "FAIL $name $detail" -ForegroundColor Red; $script:fail++ }
}
$script:called = @()
function ssh { $script:called += "ssh $args"; throw 'REAL ssh CALLED' }
function scp { $script:called += "scp $args"; throw 'REAL scp CALLED' }
$deploy = Join-Path $PSScriptRoot 'deploy.ps1'

$out = (& $deploy -Target brain -Images -Up -DryRun -Tag t1 3>$null 6>&1 | ForEach-Object { [string]$_ }) -join "`n"
Check 'brain dry run never calls ssh/scp' ($script:called.Count -eq 0) ($script:called -join ';')
$lines = $out -split "`r?`n" | Where-Object { $_ -like '[[]dry-run]*' }
Check 'dry run printed a plan' ($lines.Count -ge 15) "$($lines.Count) lines"
$scp = @($lines | Where-Object { $_ -like '*] scp*' })
$secretish = @($scp | Where-Object { $_ -match '\.key|\.crt|\.pem|\.env|\.secret|\.password|\certs|dynamic-security\.json|\data\|ca\.key' })
Check 'no secret-looking path in any scp' ($secretish.Count -eq 0) ($secretish -join ' | ')
foreach ($want in 'deploy\compose.yml', 'deploy\config', 'deploy\systemd', 'tools\setup-pi-brain.sh', 'deploy\mosquitto\mosquitto.conf', 'contracts') {
    Check "copies $want" (@($scp | Where-Object { $_ -like "*$want*" }).Count -ge 1)
}
Check 'does not copy service sources to the brain' (@($scp | Where-Object { $_ -like '*\services\*' }).Count -eq 0)
foreach ($svc in 'device-hub', 'gateway', 'agent') {
    Check "ships the $svc image tarball" (@($scp | Where-Object { $_ -like "*edge-agent-$svc-t1-arm64.tar*" }).Count -eq 1)
    Check "loads and tags $svc" (@($lines | Where-Object { $_ -like "*docker load -i*edge-agent-$svc-t1-arm64.tar*docker tag edge-agent/${svc}:t1-arm64 edge-agent/${svc}:t1*" }).Count -eq 1)
}
Check 'compose up uses EDGE_TAG, no pull' (@($lines | Where-Object { $_ -like '*EDGE_TAG=t1 docker compose up -d*' }).Count -eq 1)
Check 'ssh targets pi@pi-brain only' (@($lines | Where-Object { $_ -like '*ssh*' -and $_ -notlike '*pi@pi-brain*' }).Count -eq 0)

# other targets keep working, and -Images is brain-only
$null = (& $deploy -Target vision -DryRun 6>&1 | Out-String)
Check 'vision dry run works' ($script:called.Count -eq 0)
$threw = $false; try { & $deploy -Target vision -Images -DryRun 6>&1 | Out-Null } catch { $threw = $true }
Check '-Images is refused for non-brain targets' $threw
$threw = $false; try { & $deploy -Target brain -Tag 'bad tag;rm' -DryRun 6>&1 | Out-Null } catch { $threw = $true }
Check 'a tag with shell metacharacters is refused' $threw
Write-Host ''
if ($script:fail -gt 0) { Write-Host "$($script:fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'deploy dry-run test passed' -ForegroundColor Green
