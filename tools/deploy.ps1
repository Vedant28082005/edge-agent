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
  Uses scp (no rsync on Windows). Copies: deploy/, contracts/, services/<name>/ for the node, and for brain only
  ca.crt, server.crt, server.key and data/dynamic-security.json. NEVER copies certs\ca\ca.key, .env or node credentials.
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

# What goes where (relative to repo root).
$services = @{ brain = @('device-hub', 'agent', 'gateway'); vision = @('vision-node'); jetson = @('yolo-node') }
$items = @('contracts')
if ($Target -eq 'brain') {
    $items += 'deploy\compose.yml', 'deploy\mosquitto\mosquitto.conf', 'deploy\mosquitto\node-role.template.json'
}
foreach ($s in $services[$Target]) {
    if (Test-Path (Join-Path $root "services\$s")) { $items += "services\$s" }
}
# Secrets-bearing files for the broker: explicit allow-list, not a folder copy.
$secretFiles = @()
if ($Target -eq 'brain') {
    $secretFiles = @('deploy\mosquitto\certs\ca.crt', 'deploy\mosquitto\certs\server.crt',
                     'deploy\mosquitto\certs\server.key', 'deploy\mosquitto\data\dynamic-security.json')
}

function Invoke-Native([string]$exe, [string[]]$arguments) {
    if ($DryRun) { Write-Host "[dry-run] $exe $($arguments -join ' ')"; return }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $exe @arguments
    $code = $LASTEXITCODE; $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "$exe failed (exit $code)" }
}

Write-Host "Deploying '$Target' to $dest`:$remote"
$dirs = @("$remote/services")
if ($Target -eq 'brain') { $dirs += "$remote/deploy/mosquitto/certs", "$remote/deploy/mosquitto/data" }
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
foreach ($f in $secretFiles) {
    $src = Join-Path $root $f
    if (-not (Test-Path $src)) { Write-Warning "missing $f (run deploy\mosquitto\new-certs.ps1 / dynsec-bootstrap.ps1 first)"; continue }
    Invoke-Native 'scp' @('-q', $src, "$dest`:$remote/$($f.Replace('\', '/'))")
}
if ($Target -eq 'brain') {
    # The container runs as uid 1883 (not the ssh user), so the key must be world-readable on the node.
    # Acceptable on a single-user Pi; tighten with chown 1883 + 600 if the host is shared.
    Invoke-Native 'ssh' @($dest, "chmod 644 $remote/deploy/mosquitto/certs/server.key $remote/deploy/mosquitto/certs/*.crt; chmod 666 $remote/deploy/mosquitto/data/dynamic-security.json")
}
if ($Up) {
    $cd = if ($Target -eq 'brain') { "$remote/deploy" } else { "$remote/services/$($services[$Target][0])" }
    Invoke-Native 'ssh' @($dest, "cd $cd && docker compose up -d")
}
Write-Host 'Done.'
