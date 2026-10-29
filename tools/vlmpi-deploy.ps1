<#
.SYNOPSIS
  Ships services/vision-node (native, VLM only) to the vlmpi and runs its no-sudo installer. Starts nothing, no secrets.
.PARAMETER Remote
  user@host of the vlmpi (default vlmpi@100.122.169.70).
.PARAMETER Bind
  The vlmpi's Tailscale address the node will listen on (default 100.122.169.70). Never a wildcard.
.PARAMETER StageOnly
  Build the tarball and list it; do not touch the vlmpi.
.NOTES
  Sends: services/vision-node/vision_node, contracts/openapi/vision-node-internal.yaml, deploy/vlmpi. Nothing else.
#>
param(
    [string]$Remote = 'vlmpi@100.122.169.70',
    [string]$Bind = '100.122.169.70',
    [switch]$StageOnly
)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$root = Split-Path -Parent $here
if ($Bind -in @('0.0.0.0', '::', '')) { throw 'refusing a wildcard bind' }
if ($Remote -notmatch '^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$') { throw 'bad -Remote' }
if ($Bind -notmatch '^[0-9a-fA-F.:]+$') { throw 'bad -Bind' }

# Windows' own bsdtar: a GNU tar earlier on PATH (Git for Windows) reads 'C:\...' as host:path.
$tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
$tar = Join-Path ([System.IO.Path]::GetTempPath()) ('vlmpi-stage-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tar')
try {
    Push-Location $root
    $ErrorActionPreference = 'Continue'
    & $tarExe -cf $tar --exclude=__pycache__ --exclude=*.pyc services/vision-node/vision_node contracts/openapi/vision-node-internal.yaml deploy/vlmpi
    if ($LASTEXITCODE -ne 0) { throw 'tar failed' }
    $ErrorActionPreference = 'Stop'
    Pop-Location
    Write-Host ("Staged {0:N0} bytes:" -f (Get-Item $tar).Length)
    & $tarExe -tf $tar | Select-String -NotMatch '/$' | ForEach-Object { '  ' + $_.Line }
    if ($StageOnly) { return }

    $ErrorActionPreference = 'Continue'
    & ssh $Remote 'mkdir -p ~/edge-vision && rm -rf ~/edge-vision/stage && mkdir ~/edge-vision/stage'
    if ($LASTEXITCODE -ne 0) { throw 'ssh mkdir failed' }
    & scp $tar "${Remote}:edge-vision/stage.tar"
    if ($LASTEXITCODE -ne 0) { throw 'scp failed' }
    & ssh $Remote "tar -xf ~/edge-vision/stage.tar -C ~/edge-vision/stage && rm ~/edge-vision/stage.tar && bash ~/edge-vision/stage/deploy/vlmpi/install.sh $Bind"
    if ($LASTEXITCODE -ne 0) { throw 'install.sh failed' }
} finally {
    if (Test-Path $tar) { Remove-Item $tar -Force }
}
