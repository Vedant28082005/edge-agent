<#
.SYNOPSIS
  Builds the device-hub, gateway and agent images with buildx for linux/arm64 (the Pi) and/or linux/amd64 (local runs).
.PARAMETER Platform
  arm64 and/or amd64 (default both). arm64 images are tagged edge-agent/<svc>:<Tag>-arm64, amd64 ones <Tag>
  (that is what deploy\compose.yml and tools\e2e-smoke.ps1 use locally).
.PARAMETER Tag
  Image tag (default dev).
.PARAMETER Save
  Also write each arm64 image to deploy\dist\edge-agent-<svc>-<Tag>-arm64.tar (git-ignored); tools\deploy.ps1 -Images copies these.
.PARAMETER Smoke
  Start every built image briefly in its --mock mode (a throwaway container with a unique name, read-only root fs,
  non-root uid, tmpfs) and wait for the image's own HEALTHCHECK. Under arm64 this runs through QEMU emulation.
.PARAMETER Relock
  Regenerate deploy\docker\requirements-<svc>.lock inside an arm64 python:3.11-slim-bookworm container
  (pip --only-binary=:all:, so a missing aarch64 wheel fails instead of compiling).
.PARAMETER DryRun
  Print the docker commands without running them.
.NOTES
  No secrets: the build context is the repo root filtered by deploy\docker\Dockerfile.<svc>.dockerignore (no certs, keys,
  .env, secrets, tests, data). tools\image-scan.ps1 checks the result.
#>
param(
    [ValidateSet('arm64', 'amd64')][string[]]$Platform = @('arm64', 'amd64'),
    [string]$Tag = 'dev',
    [switch]$Save,
    [switch]$Smoke,
    [switch]$Relock,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$services = @(
    @{ Name = 'device-hub'; Uid = 10101; Args = @('--mock', '--insecure-no-auth') },
    @{ Name = 'gateway';    Uid = 10102; Args = @('--mock') },
    @{ Name = 'agent';      Uid = 10103; Args = @('--mock', '--insecure-no-auth') }
)

function Invoke-Docker([string[]]$a) {
    if ($DryRun) { Write-Host "[dry-run] docker $($a -join ' ')"; return }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & docker @a
    $code = $LASTEXITCODE; $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "docker $($a[0]) failed (exit $code)" }
}

if ($Relock) {
    foreach ($s in $services) {
        $n = $s.Name
        $req = Join-Path $root "services\$n\requirements.txt"
        $out = Join-Path $root "deploy\docker\requirements-$n.lock"
        $cmd = "python -m venv /tmp/v && /tmp/v/bin/pip install -q --default-timeout 120 --retries 5 --only-binary=:all: -r /req.txt && " +
               "{ echo '# Pinned for linux/aarch64, CPython 3.11 (resolved with pip --only-binary=:all: from services/$n/requirements.txt, then pip freeze).'; " +
               "echo '# Regenerate: tools\build-images.ps1 -Relock   (never edit by hand; every entry is a wheel, no compilers needed)'; " +
               "/tmp/v/bin/pip freeze --all | grep -v -E '^(pip|setuptools|wheel)=='; } > /out/requirements-$n.lock"
        Invoke-Docker @('run', '--rm', '--platform', 'linux/arm64', '-v', "${req}:/req.txt:ro", '-v', "$(Join-Path $root 'deploy\docker'):/out", 'python:3.11-slim-bookworm', 'sh', '-c', $cmd)
    }
}

$failed = @()
foreach ($p in $Platform) {
    foreach ($s in $services) {
        $n = $s.Name
        $img = if ($p -eq 'arm64') { "edge-agent/${n}:$Tag-arm64" } else { "edge-agent/${n}:$Tag" }
        Write-Host "== build $img (linux/$p)"
        Invoke-Docker @('buildx', 'build', '--platform', "linux/$p", '--load', '-f', "deploy/docker/Dockerfile.$n", '-t', $img, '--progress', 'plain', '.') | Out-Null
        if ($Save -and $p -eq 'arm64') {
            $dist = Join-Path $root 'deploy\dist'
            if (-not $DryRun) { New-Item -ItemType Directory -Force $dist | Out-Null }
            Invoke-Docker @('save', '-o', (Join-Path $dist "edge-agent-$n-$Tag-arm64.tar"), $img)
        }
        if ($Smoke) {
            $cname = "s11-smoke-$n-" + (-join ((1..6) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) }))
            Write-Host "   smoke: $cname (mock mode, read-only fs, uid $($s.Uid))"
            if ($DryRun) {
                Write-Host "[dry-run] docker run -d --name $cname --platform linux/$p --read-only --tmpfs /tmp --user $($s.Uid):$($s.Uid) $img $($s.Args -join ' ')"
                continue
            }
            $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            docker run -d --name $cname --platform "linux/$p" --read-only --tmpfs /tmp --user "$($s.Uid):$($s.Uid)" --cap-drop ALL --security-opt no-new-privileges:true --memory 512m $img @($s.Args) | Out-Null
            $status = 'starting'
            $deadline = (Get-Date).AddMinutes(5)
            while ((Get-Date) -lt $deadline -and $status -in 'starting') {
                Start-Sleep -Seconds 5
                $status = (docker inspect -f '{{.State.Health.Status}}' $cname 2>$null)
                if (-not $status) { $status = 'gone' }
                $running = docker inspect -f '{{.State.Running}}' $cname 2>$null
                if ($running -ne 'true') { $status = 'exited'; break }
            }
            if ($status -eq 'healthy') { Write-Host "   PASS $n ($p): healthy" -ForegroundColor Green }
            else {
                Write-Host "   FAIL $n ($p): $status" -ForegroundColor Red; $failed += "$n/$p"
                docker logs --tail 25 $cname 2>&1 | ForEach-Object { Write-Host "     $_" }
            }
            docker rm -f $cname | Out-Null          # only the container started here
            $ErrorActionPreference = $prev
        }
    }
}
if ($failed.Count) { Write-Host "smoke failed: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host 'Done.'
