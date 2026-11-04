<#
.SYNOPSIS
  Ships services/vision-node (native, VLM only) to the vlmpi and runs its no-sudo installer. Starts nothing, no secrets.
.PARAMETER Remote
  user@host of the vlmpi (default vlmpi@100.122.169.70).
.PARAMETER Bind
  The vlmpi's Tailscale address the node will listen on (default 100.122.169.70). Never a wildcard.
.PARAMETER StageOnly
  Stage, normalise to LF, run `bash -n` and list the tarball; do not touch the vlmpi.
.NOTES
  Sends: services/vision-node/vision_node, contracts/openapi/vision-node-internal.yaml, deploy/vlmpi. Nothing else.
  Every text file is converted to LF while staging (this repo lives on Windows). Every .sh file must pass `bash -n`
  locally (Git for Windows bash) before anything is copied, and again on the vlmpi right before it runs.
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
$id = [guid]::NewGuid().ToString('N').Substring(0, 8)
$tmp = [System.IO.Path]::GetTempPath()
$tar = Join-Path $tmp "vlmpi-stage-$id.tar"
$stage = Join-Path $tmp "vlmpi-stage-$id"
$textExt = '.sh', '.service', '.template', '.txt', '.md', '.py', '.yaml', '.yml', '.json'

function Find-Bash {
    # Git for Windows' bash; NOT C:\Windows\System32\bash.exe (that one starts WSL).
    foreach ($c in @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe", "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe")) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    $g = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($g) {
        $b = Join-Path (Split-Path (Split-Path $g.Source)) 'bin\bash.exe'
        if (Test-Path $b) { return $b }
    }
    return $null
}

try {
    # 1. stage a copy with LF endings
    foreach ($item in @('services\vision-node\vision_node', 'contracts\openapi\vision-node-internal.yaml', 'deploy\vlmpi')) {
        $src = Join-Path $root $item
        $dst = Join-Path $stage $item
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Copy-Item $src $dst -Recurse -Force
    }
    Get-ChildItem $stage -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
    Get-ChildItem $stage -Recurse -File | Where-Object { $textExt -contains $_.Extension.ToLower() } | ForEach-Object {
        $b = [System.IO.File]::ReadAllBytes($_.FullName)
        $out = New-Object System.Collections.Generic.List[byte] $b.Length
        for ($i = 0; $i -lt $b.Length; $i++) {
            if ($b[$i] -eq 13 -and $i + 1 -lt $b.Length -and $b[$i + 1] -eq 10) { continue }
            $out.Add($b[$i])
        }
        if ($out.Count -ne $b.Length) { [System.IO.File]::WriteAllBytes($_.FullName, $out.ToArray()) }
        if ($out.Contains([byte]13)) { throw "stray CR left in $($_.Name)" }
    }

    # 2. syntax check every shell script locally
    $scripts = @(Get-ChildItem $stage -Recurse -File -Filter *.sh)
    if (-not $scripts) { throw 'no .sh files staged' }
    $bash = Find-Bash
    if ($bash) {
        foreach ($s in $scripts) {
            $ErrorActionPreference = 'Continue'
            $msg = & $bash -n $s.FullName 2>&1
            $rc = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            if ($rc -ne 0) { throw "bash -n failed for $($s.Name): $msg" }
        }
        Write-Host ("bash -n: OK ({0} script(s), {1})" -f $scripts.Count, $bash)
    } else {
        Write-Host 'bash -n: Git bash not found locally; it will run on the vlmpi before the installer'
    }

    # 3. tarball from the normalised copy
    $ErrorActionPreference = 'Continue'
    & $tarExe -cf $tar -C $stage services contracts deploy
    $rc = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($rc -ne 0) { throw 'tar failed' }
    Write-Host ("Staged {0:N0} bytes:" -f (Get-Item $tar).Length)
    & $tarExe -tf $tar | Select-String -NotMatch '/$' | ForEach-Object { '  ' + $_.Line }
    if ($StageOnly) { return }

    # 4. ship; the installer only runs if `bash -n` also passes on the vlmpi
    $ErrorActionPreference = 'Continue'
    & ssh $Remote 'mkdir -p ~/edge-vision && rm -rf ~/edge-vision/stage && mkdir ~/edge-vision/stage'
    if ($LASTEXITCODE -ne 0) { throw 'ssh mkdir failed' }
    & scp $tar "${Remote}:edge-vision/stage.tar"
    if ($LASTEXITCODE -ne 0) { throw 'scp failed' }
    & ssh $Remote "tar -xf ~/edge-vision/stage.tar -C ~/edge-vision/stage && rm ~/edge-vision/stage.tar && bash -n ~/edge-vision/stage/deploy/vlmpi/install.sh && bash ~/edge-vision/stage/deploy/vlmpi/install.sh $Bind"
    if ($LASTEXITCODE -ne 0) { throw 'remote install failed (see the output above; nothing was started)' }
} finally {
    if (Test-Path $tar) { Remove-Item $tar -Force }
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
}
