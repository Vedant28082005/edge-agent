<#
.SYNOPSIS
  Reports free RAM, temperature, throttle status and disk for each node over ssh.
  Usage: tools\node-check.ps1 [-Node brain,vision,jetson]
.NOTES
  Pi throttle status via `vcgencmd get_throttled` (0x0 = fine). The Jetson has no vcgencmd;
  temperature comes from /sys/class/thermal and throttle is reported as n/a.
  [measured] values come from the node at call time; thresholds follow the plan (MemAvailable >= 500 MB).
#>
param([ValidateSet('brain', 'vision', 'jetson')][string[]]$Node = @('brain', 'vision', 'jetson'))
$ErrorActionPreference = 'Stop'
$nodes = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'nodes.psd1')

# One remote command, plain POSIX sh, key=value output.
$remoteCmd = @'
echo mem_avail_mb=$(awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo)
echo mem_total_mb=$(awk '/MemTotal/{printf "%d",$2/1024}' /proc/meminfo)
echo temp_c=$(awk '{printf "%.1f",$1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
if command -v vcgencmd >/dev/null 2>&1; then echo throttled=$(vcgencmd get_throttled | cut -d= -f2); else echo throttled=n/a; fi
echo disk=$(df -h / | awk 'NR==2{print $4" free of "$2" ("$5" used)"}')
echo load=$(cut -d' ' -f1-3 /proc/loadavg)
'@

$rows = foreach ($name in $Node) {
    $n = $nodes[$name]
    $dest = "$($n.User)@$($n.Host)"
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = ssh -o BatchMode=yes -o ConnectTimeout=5 $dest $remoteCmd 2>&1
    $code = $LASTEXITCODE; $ErrorActionPreference = $prev
    if ($code -ne 0) {
        [pscustomobject]@{ Node = $name; Status = "UNREACHABLE"; FreeRAM_MB = ''; Temp_C = ''; Throttled = ''; Disk = ''; Load = '' }
        continue
    }
    $kv = @{}; $out | ForEach-Object { $k, $v = "$_" -split '=', 2; if ($v -ne $null) { $kv[$k] = $v } }
    $warn = @()
    if ([int]$kv['mem_avail_mb'] -lt 500) { $warn += 'LOW RAM' }
    if ($kv['temp_c'] -and [double]$kv['temp_c'] -ge 75) { $warn += 'HOT' }
    if ($kv['throttled'] -notin @('0x0', 'n/a')) { $warn += 'THROTTLED' }
    [pscustomobject]@{
        Node = $name; Status = $(if ($warn) { $warn -join ',' } else { 'ok' })
        FreeRAM_MB = "$($kv['mem_avail_mb'])/$($kv['mem_total_mb'])"; Temp_C = $kv['temp_c']
        Throttled = $kv['throttled']; Disk = $kv['disk']; Load = $kv['load']
    }
}
$rows | Format-Table -AutoSize
if ($rows | Where-Object { $_.Status -ne 'ok' }) { exit 1 }
