<#
.SYNOPSIS
  Provisions a simulated node: the same gateway call a real board's provisioning makes
  (POST /admin/provision-sessions), but the bundle is written to a git-ignored folder instead of the serial port.

.DESCRIPTION
  Writes tools\sim-node\.sim\<node_id>\ca.pem and <node_id>.cred (username=/password= lines, MQTT user == node_id).
  The admin password (and the Wi-Fi password, if you pass -WifiSsid) are prompted for as SecureString; they are never
  taken from files, arguments or the environment, and neither the admin password nor the MQTT password is ever printed.

  -NodeId is either a real esp32-<12 hex> id or a label such as sim-fan-01. A label maps to
  esp32-<first 12 hex of sha256(label)>, the same rule sim_node.py uses, because the hub only accepts esp32-<12 hex>.

  The provisioning session expires after about 10 minutes: start the sim right after this script, the node must
  announce within that time or the pending session (and its MQTT user) is cleaned up.

.EXAMPLE
  .\provision-sim.ps1 -NodeId sim-fan-01 -GatewayUrl https://pi-brain.tailnet.ts.net -AdminUser admin
#>
param(
    [Parameter(Mandatory)] [string] $NodeId,
    [Parameter(Mandatory)] [string] $GatewayUrl,
    [string] $AdminUser = 'admin',
    [string] $WifiProfile = '',   # saved profile name (GET /admin/wifi-profiles)
    [string] $WifiSsid = '',      # alternative: inline Wi-Fi, password prompted. Neither: a placeholder the sim ignores
    [string] $SimDir = '',
    [string] $Profile = '',                 # light|fan|curtain|dht|relay4|sensors|room: recorded in .sim\nodes.json for start-sims.bat
    [switch] $PassThru,                     # emit the node id on the pipeline (for start-room.ps1)
    [SecureString] $AdminPassword = $null   # for start-room.ps1, which asks once; never read from a file or the environment
)
$ErrorActionPreference = 'Stop'

function Read-Secret([string] $prompt) {
    $s = Read-Host -Prompt $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

# --- node id (must equal sim_node.resolve_node_id)
if ($NodeId -match '^esp32-[0-9a-f]{12}$') { $nid = $NodeId }
elseif ($NodeId -match '^[A-Za-z0-9._-]{1,48}$') {
    $sha = [Security.Cryptography.SHA256]::Create()
    $hex = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($NodeId)) | ForEach-Object { $_.ToString('x2') })
    $nid = 'esp32-' + $hex.Substring(0, 12)
} else { throw "NodeId must be esp32-<12 hex> or a label of [A-Za-z0-9._-] (max 48 chars)" }

if (-not $SimDir) { $SimDir = Join-Path $PSScriptRoot '.sim' }
$dir = Join-Path $SimDir $nid
$base = $GatewayUrl.TrimEnd('/')

# --- admin login
if ($AdminPassword) {
    $b0 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AdminPassword)
    try { $adminPass = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b0) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b0) }
} else { $adminPass = Read-Secret "Admin password for '$AdminUser'" }
$wifiPass = $null
if ($WifiSsid) { $wifiPass = Read-Secret 'Wi-Fi password (empty for an open network)' }
try {
    $login = Invoke-RestMethod -Method Post -Uri "$base/auth/login" -ContentType 'application/json' `
        -Body (@{ username = $AdminUser; password = $adminPass } | ConvertTo-Json)
} catch { throw "Admin login failed (HTTP $($_.Exception.Response.StatusCode.value__))" }
finally { $adminPass = $null }
if ($login.role -ne 'admin') { throw "User '$AdminUser' is '$($login.role)', not admin" }
$hdr = @{ Authorization = "Bearer $($login.access_token)" }

# --- provision session
$req = @{ node_id = $nid }
if ($WifiProfile) { $req.wifi_profile = $WifiProfile }
elseif ($WifiSsid) { $req.wifi = @{ ssid = $WifiSsid; password = $wifiPass } }
else { $req.wifi = @{ ssid = 'sim-no-wifi'; password = 'sim-no-wifi' } }   # placeholder, the sim never joins Wi-Fi
try {
    $sess = Invoke-RestMethod -Method Post -Uri "$base/admin/provision-sessions" -Headers $hdr `
        -ContentType 'application/json' -Body ($req | ConvertTo-Json -Depth 4)
} catch { throw "Provision session failed (HTTP $($_.Exception.Response.StatusCode.value__))" }
finally { $wifiPass = $null; $req = $null }

$b = $sess.bundle
if (-not $b.ca_pem -or -not $b.mqtt.password -or $b.mqtt.username -ne $nid) {
    throw 'Bundle is missing ca_pem / mqtt.password, or mqtt.username is not the node id'
}

# --- write to the git-ignored folder, readable by this user only
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ascii = New-Object Text.ASCIIEncoding
[IO.File]::WriteAllText((Join-Path $dir 'ca.pem'), $b.ca_pem, $ascii)
[IO.File]::WriteAllText((Join-Path $dir "$nid.cred"), "username=$($b.mqtt.username)`npassword=$($b.mqtt.password)`n", $ascii)
& icacls $dir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

if ($Profile) {
    . (Join-Path $PSScriptRoot 'sim-common.ps1')
    $env:SIM_DIR = $SimDir
    & (Get-SimPython) (Join-Path $PSScriptRoot 'simctl.py') register --label $NodeId --profile $Profile --node-id $nid --gateway $base | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not update .sim\nodes.json; register it with: python simctl.py register --label ... --profile ...' }
} else {
    Write-Warning 'No -Profile given: this node is not in .sim\nodes.json, so start-sims.bat cannot tell which profile to run. Re-run with -Profile, or: python simctl.py register --label <label> --profile <profile> --node-id <id>'
}
$exp = [DateTimeOffset]::FromUnixTimeSeconds([int64]$sess.expires_at).LocalDateTime.ToString('HH:mm:ss')
Write-Host "node id   : $nid"
Write-Host "broker    : $($b.mqtt.host):$($b.mqtt.port)  (bundle value; sim_node.py defaults to 192.168.29.89:8883, override with --host)"
Write-Host "written to: $dir  (ca.pem, $nid.cred; git-ignored)"
Write-Host "REMINDER  : the provisioning session expires in about 10 minutes (at $exp). Start the sim now:"
Write-Host "            python sim_node.py --profile <profile> --node-id $NodeId   (or double-click start-sims.bat)"
$b = $null; $sess = $null
if ($PassThru) { $nid }
