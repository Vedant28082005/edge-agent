<#
.SYNOPSIS
  Generates a private CA and a broker server certificate into deploy/mosquitto/certs/ (git-ignored).
.PARAMETER LanIp
  LAN IP of the broker host (what the ESP32s connect to). Goes into the SANs.
.PARAMETER TailnetName
  Tailnet DNS name of the broker host (e.g. pi-brain.tailnet-name.ts.net). Goes into the SANs.
.PARAMETER ExtraDns / ExtraIp
  Optional additional SANs. localhost and 127.0.0.1 are always included (local testing).
.PARAMETER ServerDays
  Validity of the server certificate in days (default 1095, about 3 years).
.PARAMETER Force
  Regenerate the server certificate even if it exists (the CA is kept unless -NewCa).
.NOTES
  Needs openssl on PATH (Git for Windows ships one). Layout:
    certs/ca/ca.key, ca.crt   CA (ca.key never leaves this PC, deploy.ps1 does not copy it)
    certs/ca.crt              CA cert (broker truststore + goes into the BLE bundle)
    certs/server.crt, server.key   broker cert and key (Windows ACL: current user only)
  Ownership by the container user (uid 1883, mode 600) cannot be set on Windows; tools\deploy.ps1 does it on the
  Pi (sudo chown 1883:1883 + chmod 600). On Docker Desktop the bind mount ignores POSIX ownership.
  Server cert: RSA 2048, SHA-256, -ServerDays (default 1095 = 3 years). CA: RSA 4096, 10 years (3650 days).
  Renewing the server cert under the SAME CA needs no board re-provisioning (boards trust the CA).
#>
param(
    [Parameter(Mandatory)][string]$LanIp,
    [Parameter(Mandatory)][string]$TailnetName,
    [string[]]$ExtraDns = @(),
    [string[]]$ExtraIp = @(),
    [ValidateRange(30, 3650)][int]$ServerDays = 1095,
    [switch]$Force,
    [switch]$NewCa
)
$ErrorActionPreference = 'Stop'
$ssl = (Get-Command openssl -ErrorAction Stop).Source
$certs = Join-Path $PSScriptRoot 'certs'
$caDir = Join-Path $certs 'ca'
New-Item -ItemType Directory -Force $caDir | Out-Null

function Invoke-Ssl {
    $ErrorActionPreference = 'Continue'   # PS 5.1: native stderr must not abort
    & $ssl @args 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "openssl $($args[0]) failed (exit $LASTEXITCODE)" }
}

$caKey = Join-Path $caDir 'ca.key'
$caCrt = Join-Path $caDir 'ca.crt'
if ($NewCa -or -not (Test-Path $caCrt)) {
    Write-Host 'Generating CA ...'
    Invoke-Ssl genrsa -out $caKey 4096
    Invoke-Ssl req -x509 -new -key $caKey -sha256 -days 3650 -subj '/CN=edge-agent private CA/O=edge-agent' `
        -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' -addext 'keyUsage=critical,keyCertSign,cRLSign' -out $caCrt
    $Force = $true
}
Copy-Item $caCrt (Join-Path $certs 'ca.crt') -Force

$srvCrt = Join-Path $certs 'server.crt'
if ($Force -or -not (Test-Path $srvCrt)) {
    Write-Host 'Generating server certificate ...'
    $dns = @('localhost', $TailnetName) + $ExtraDns | Select-Object -Unique
    $ips = @('127.0.0.1', $LanIp) + $ExtraIp | Select-Object -Unique
    $san = (($dns | ForEach-Object { "DNS:$_" }) + ($ips | ForEach-Object { "IP:$_" })) -join ','
    $srvKey = Join-Path $certs 'server.key'
    $csr = Join-Path $certs 'server.csr'
    Invoke-Ssl genrsa -out $srvKey 2048
    Invoke-Ssl req -new -key $srvKey -subj "/CN=$TailnetName/O=edge-agent" -out $csr
    $ext = Join-Path $certs 'server.ext'
    Set-Content -Path $ext -Encoding ascii -Value @(
        'basicConstraints=CA:FALSE',
        'keyUsage=critical,digitalSignature,keyEncipherment',
        'extendedKeyUsage=serverAuth',
        "subjectAltName=$san")
    Invoke-Ssl x509 -req -in $csr -CA $caCrt -CAkey $caKey -CAcreateserial -out $srvCrt -days $ServerDays -sha256 -extfile $ext
    Remove-Item $csr, $ext -Force
    Write-Host "SANs: $san"
}
# Restrict private keys to the current Windows user (strip inherited ACEs).
foreach ($k in @($caKey, (Join-Path $certs 'server.key'))) {
    if (Test-Path $k) {
        $ErrorActionPreference = 'Continue'
        icacls $k /inheritance:r /grant:r "$($env:USERNAME):(R,W)" 2>&1 | Out-Null
        $ErrorActionPreference = 'Stop'
    }
}
Write-Host "Done. Certificates in $certs (git-ignored). Never commit them."
