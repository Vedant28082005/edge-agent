<#
.SYNOPSIS
  Smoke test of the vision-node on the vlmpi (yes/no, short, JSON with timings) using a real image.
.PARAMETER Image
  One or more jpg/png/webp files on this PC.
.PARAMETER Via
  brain (default): the test runs ON pi-brain with sudo (you type the brain sudo password), so the API secret never leaves the
  brain and the path brain -> vlmpi:8300 is exactly the one the agent will use. Needs python3 there (stock Debian has it).
  local: run on this PC; needs -SecretFile (a file you placed yourself; the script never fetches the secret).
.NOTES
  The secret is never printed, passed on a command line or stored by this script. Temporary files on the brain are removed.
#>
param(
    [Parameter(Mandatory)][string[]]$Image,
    [ValidateSet('brain', 'local')][string]$Via = 'brain',
    [string]$SecretFile,
    [string]$Brain = 'homebench@100.82.120.100',
    [string]$Url = 'http://100.122.169.70:8300',
    [string]$Question = 'Is there a person in the image?'
)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$script = Join-Path $here 'vision-node-smoke.py'
foreach ($i in $Image) { if (-not (Test-Path $i)) { throw "image not found: $i" } }
if ($Url -notmatch '^http://[0-9A-Za-z.\-:]+$') { throw 'bad -Url' }
if ($Question -match '["`$\\]') { throw 'keep -Question free of quotes, backticks, $ and backslashes' }

if ($Via -eq 'local') {
    if (-not $SecretFile) { throw '-Via local needs -SecretFile' }
    $py = Join-Path (Split-Path -Parent $here) '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = 'python' }
    $imgArgs = @(); foreach ($i in $Image) { $imgArgs += @('--image', $i) }
    & $py $script --url $Url --secret-file $SecretFile --question $Question @imgArgs
    exit $LASTEXITCODE
}

if ($Brain -notmatch '^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$') { throw 'bad -Brain' }
$tag = [guid]::NewGuid().ToString('N').Substring(0, 8)
$rdir = "/tmp/vn-smoke-$tag"
$ErrorActionPreference = 'Continue'
try {
    & ssh $Brain "mkdir -m 700 $rdir"
    if ($LASTEXITCODE -ne 0) { throw 'ssh mkdir failed' }
    & scp -q $script "${Brain}:$rdir/smoke.py"
    if ($LASTEXITCODE -ne 0) { throw 'scp script failed' }
    $remoteImgs = @(); $n = 0
    foreach ($i in $Image) { $n++; & scp -q $i "${Brain}:$rdir/img$n"; if ($LASTEXITCODE -ne 0) { throw 'scp image failed' }; $remoteImgs += "--image $rdir/img$n" }
    # sudo only to read the root-owned secret; the file path (not its content) is the argument
    & ssh -t $Brain "sudo python3 $rdir/smoke.py --url $Url --secret-file /etc/edge-agent/secrets/vision-node-api.secret --question '$Question' $($remoteImgs -join ' ')"
    $rc = $LASTEXITCODE
} finally {
    & ssh $Brain "rm -rf $rdir" | Out-Null
}
exit $rc
