<#
.SYNOPSIS
  Copies the repo pieces a node needs to it over ssh/scp and (optionally) restarts its compose stack.
  Targets: brain (pi-brain), vision (pi-vision), jetson (jetson-yolo). Hosts/users in tools\nodes.psd1.
.PARAMETER Target
  brain | vision | jetson
.PARAMETER Images
  brain and vision: copy the arm64 image tarballs built by `tools\build-images.ps1 -Save` (deploy\dist) to the node,
  `docker load` them, tag them edge-agent/<svc>:<Tag> and delete the tarballs. Nothing is built or pulled on the Pi.
.PARAMETER Tag
  Image tag used with -Images and -Up (default dev; must match build-images.ps1 -Tag).
.PARAMETER Up
  After copying, run `docker compose up -d` on the node (brain: with EDGE_TAG=<Tag>).
.PARAMETER DryRun
  Print what would be copied/run without touching the network.
.NOTES
  Routine deploys need NO root and carry NO secrets. They copy only code and non-secret config:
  contracts/, services/<name>/ (vision/jetson), and for brain deploy/compose.yml, mosquitto.conf, the role template,
  the chrony config, deploy/config (non-secret service JSON), deploy/systemd (llama-server unit + env TEMPLATE) and
  tools/setup-pi-brain.sh. The brain's three services ship as images (-Images), not as source.

  Certificates, the broker key and the dynsec file are installed ONCE by hand with tools/setup-pi-brain.sh
  (see deploy/README.md, "One-time Pi setup"). This script never copies certs, keys, .env or credentials.
#>
param(
    [Parameter(Mandatory)][ValidateSet('brain', 'vision', 'jetson')][string]$Target,
    [switch]$Images,
    [ValidatePattern('^[A-Za-z0-9_.-]{1,40}$')][string]$Tag = 'dev',
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
$services = @{ brain = @(); vision = @('vision-node'); jetson = @('yolo-node') }   # brain services are images, see -Images
$imageSets = @{ brain = @('device-hub', 'gateway', 'agent'); vision = @('vision-node') }
if ($Images -and -not $imageSets.ContainsKey($Target)) { throw '-Images applies to the brain and vision targets only' }
$items = @('contracts')
if ($Target -eq 'brain') {
    $items += 'deploy\compose.yml', 'deploy\mosquitto\mosquitto.conf', 'deploy\mosquitto\node-role.template.json',
              'deploy\chrony', 'deploy\config', 'deploy\systemd', 'tools\setup-pi-brain.sh'
}
if ($Target -eq 'vision') {
    # compose + native-service units + non-secret config; the image ships via -Images; the verifier/secret setup is by hand (deploy/README.md)
    $items += 'deploy\compose.vision.yml', 'deploy\systemd', 'deploy\config\vision-node.json'
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
if ($Target -eq 'vision') { $dirs += "$remote/deploy/config" }
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
if ($Images) {
    # Pre-flight: every tarball must exist (a dry run only warns).
    $dist = Join-Path $root 'deploy\dist'
    Invoke-Native 'ssh' @($dest, "mkdir -p $remote/images")
    foreach ($svc in $imageSets[$Target]) {
        $tar = Join-Path $dist "edge-agent-$svc-$Tag-arm64.tar"
        if (-not (Test-Path $tar)) {
            if ($DryRun) { Write-Warning "missing $tar (run tools\build-images.ps1 -Tag $Tag -Save first)" }
            else { throw "missing $tar (run tools\build-images.ps1 -Tag $Tag -Save first)" }
        }
        Invoke-Native 'scp' @('-q', $tar, "$dest`:$remote/images/")
        $name = "edge-agent-$svc-$Tag-arm64.tar"
        Invoke-Native 'ssh' @($dest, "docker load -i $remote/images/$name && docker tag edge-agent/${svc}:$Tag-arm64 edge-agent/${svc}:$Tag && rm -f $remote/images/$name")
    }
}
if ($Up) {
    if ($Target -eq 'brain') {
        Invoke-Native 'ssh' @($dest, "cd $remote/deploy && EDGE_TAG=$Tag docker compose up -d --remove-orphans")
    } elseif ($Target -eq 'vision') {
        Invoke-Native 'ssh' @($dest, "cd $remote/deploy && EDGE_TAG=$Tag docker compose -f compose.vision.yml up -d --remove-orphans")
    } else {
        Invoke-Native 'ssh' @($dest, "cd $remote/services/$($services[$Target][0]) && docker compose up -d")
    }
}
Write-Host 'Done.'
