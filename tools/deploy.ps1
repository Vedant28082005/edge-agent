<#
.SYNOPSIS
  Copies the repo pieces a node needs to it over ssh/scp and (optionally) restarts its compose stack.
  Targets: brain (pi-brain), vision (pi-vision), jetson (jetson-yolo). Hosts/users in tools\nodes.psd1.
.PARAMETER Target
  brain | vision | jetson
.PARAMETER Up
  After copying, run `docker compose up -d` on the node.
.PARAMETER DryRun
  Print what would be copied/run without touching the network.
.NOTES
  Routine deploys need NO root and carry NO secrets. They copy only code and non-secret config:
  contracts/, services/<name>/, and for brain also deploy/compose.yml, mosquitto.conf, the role template,
  the chrony config and tools/setup-pi-brain.sh.

  Certificates, the broker key and the dynsec file are installed ONCE by hand with tools/setup-pi-brain.sh
  (see deploy/README.md, "One-time Pi setup"). This script never copies certs, keys, .env or credentials.
#>
param(
    [Parameter(Mandatory)][ValidateSet('brain', 'vision', 'jetson')][string]$Target,
    [switch]$Up,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$nodes = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'nodes.psd1')
$n = $nodes[$Target]
$dest = "$($n.User)@$($n.Host)"
$remote = $n.Dir

# What goes where (relative to repo root). Nothing secret is in this list.
$services = @{ brain = @('device-hub', 'agent', 'gateway'); vision = @('vision-node'); jetson = @('yolo-node') }
$items = @('contracts')
if ($Target -eq 'brain') {
    $items += 'deploy\compose.yml', 'deploy\mosquitto\mosquitto.conf', 'deploy\mosquitto\node-role.template.json',
              'deploy\chrony', 'tools\setup-pi-brain.sh'
}
foreach ($s in $services[$Target]) {
    if (Test-Path (Join-Path $root "services\$s")) { $items += "services\$s" }
}

function Invoke-Native([string]$exe, [string[]]$arguments) {
    if ($DryRun) { Write-Host "[dry-run] $exe $($arguments -join ' ')"; return }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $exe @arguments
    $code = $LASTEXITCODE; $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "$exe failed (exit $code)" }
}

Write-Host "Deploying '$Target' to $dest`:$remote"
$dirs = @("$remote/services", "$remote/tools")
if ($Target -eq 'brain') { $dirs += "$remote/deploy/mosquitto" }
Invoke-Native 'ssh' @($dest, "mkdir -p $($dirs -join ' ')")

foreach ($i in $items) {
    $src = Join-Path $root $i
    if (-not (Test-Path $src)) { Write-Warning "skip missing $i"; continue }
    $rel = $i.Replace('\', '/')
    $isDir = (Get-Item $src).PSIsContainer
    $parent = (Split-Path $rel -Parent).Replace('\', '/')
    $rdir = if ($parent) { "$remote/$parent/" } else { "$remote/" }
    $flags = @(); if ($isDir) { $flags = @('-r') }
    Invoke-Native 'scp' ($flags + @('-q', $src, "$dest`:$rdir"))
}
if ($Up) {
    $cd = if ($Target -eq 'brain') { "$remote/deploy" } else { "$remote/services/$($services[$Target][0])" }
    Invoke-Native 'ssh' @($dest, "cd $cd && docker compose up -d")
}
Write-Host 'Done.'
