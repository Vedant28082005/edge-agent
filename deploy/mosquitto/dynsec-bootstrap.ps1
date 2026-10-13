<#
.SYNOPSIS
  Creates the initial dynamic-security config with the privileged hub admin account.
  Run once (or with -Force to reset ALL users/roles). Needs Docker (uses the mosquitto image's mosquitto_ctrl).
.DESCRIPTION
  Writes data/dynamic-security.json (git-ignored) and a random hub admin password into .env (git-ignored).
  The password is never printed. The default ACL is tightened to deny-all so a node can only do what its role allows.
  Per-node users/roles are created later from node-role.template.json (dynsec-node-user.ps1, or the hub's dynsec client).
#>
param(
    [string]$AdminUser = 'hub-admin',
    [string]$Image = 'eclipse-mosquitto:2.0.20',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$data = Join-Path $PSScriptRoot 'data'
$cfg  = Join-Path $data 'dynamic-security.json'
$envf = Join-Path $PSScriptRoot '.env'
if ((Test-Path $cfg) -and -not $Force) { Write-Host "dynamic-security.json exists; use -Force to reset."; return }
New-Item -ItemType Directory -Force $data | Out-Null

$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$pw = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','A').Replace('/','b')

Remove-Item $cfg -Force -ErrorAction SilentlyContinue
$ErrorActionPreference = 'Continue'
docker run --rm -v "${data}:/mosquitto/data" $Image mosquitto_ctrl dynsec init /mosquitto/data/dynamic-security.json $AdminUser $pw 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $cfg)) { throw 'mosquitto_ctrl dynsec init failed (is Docker running?)' }
$ErrorActionPreference = 'Stop'

# Tighten defaults: deny everything not granted by a role.
$j = Get-Content $cfg -Raw | ConvertFrom-Json
foreach ($name in @($j.defaultACLAccess.PSObject.Properties.Name)) { $j.defaultACLAccess.$name = $false }
# The init 'admin' role can receive/subscribe everywhere but not publish outside $CONTROL: the hub must publish cmds.
$adminRole = $j.roles | Where-Object { $_.rolename -eq 'admin' }
$adminRole.acls += [pscustomobject]@{ acltype = 'publishClientSend'; topic = '#'; allow = $true }
# Containers run as uid 1883; make the file readable/writable for them via the bind mount.
$j | ConvertTo-Json -Depth 20 | Set-Content -Encoding ascii $cfg

Set-Content -Encoding ascii -Path $envf -Value @("HUB_ADMIN_USER=$AdminUser", "HUB_ADMIN_PASSWORD=$pw")
Write-Host "Created $cfg and $envf (both git-ignored). Admin user: $AdminUser. Password is in .env only."
