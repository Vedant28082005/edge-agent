<#
.SYNOPSIS
  Creates (or with -Remove deletes) an MQTT user + role for one node via the dynamic-security plugin.
  Intended for local testing and manual provisioning; the hub's dynsec client does the same in production.
.DESCRIPTION
  Role ACLs come from node-role.template.json with {{node_id}} substituted, so a node can only
  publish/subscribe on home/v1/nodes/<node_id>/#. The node's password is generated randomly and written to
  certs/nodes/<node_id>.cred (git-ignored), never printed.
  Talks to the running broker container over TLS using the generated CA and the hub admin from .env.
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^esp32-[0-9a-f]{12}$')][string]$NodeId,
    [string]$Container = 'edge-mosquitto',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$envf = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path $envf)) { throw 'Run dynsec-bootstrap.ps1 first (.env missing).' }
$e = @{}; Get-Content $envf | ForEach-Object { $k, $v = $_ -split '=', 2; $e[$k] = $v }

function Invoke-Ctrl {
    $ErrorActionPreference = 'Continue'
    $out = docker exec $Container mosquitto_ctrl -h localhost -p 8883 --cafile /mosquitto/certs/ca.crt `
        -u $e['HUB_ADMIN_USER'] -P $e['HUB_ADMIN_PASSWORD'] dynsec @args 2>&1
    if ($LASTEXITCODE -ne 0) { throw "mosquitto_ctrl dynsec $($args -join ' ') failed: $out" }
}

$role = "node-$NodeId"
if ($Remove) {
    Invoke-Ctrl deleteClient $NodeId
    Invoke-Ctrl deleteRole $role
    Write-Host "Removed $NodeId and role $role."
    return
}

$tpl = Get-Content (Join-Path $PSScriptRoot 'node-role.template.json') -Raw
$r = ($tpl -replace '\{\{node_id\}\}', $NodeId) | ConvertFrom-Json
Invoke-Ctrl createRole $r.rolename
foreach ($a in $r.acls) {
    Invoke-Ctrl addRoleACL $r.rolename $a.acltype $a.topic ($(if ($a.allow) { 'allow' } else { 'deny' })) $a.priority
}
$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$pw = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','A').Replace('/','b')
Invoke-Ctrl createClient $NodeId -c $NodeId -p $pw
Invoke-Ctrl addClientRole $NodeId $r.rolename 1

$dir = Join-Path $PSScriptRoot 'certs\nodes'
New-Item -ItemType Directory -Force $dir | Out-Null
Set-Content -Encoding ascii -Path (Join-Path $dir "$NodeId.cred") -Value @("username=$NodeId", "password=$pw")
Write-Host "Created $NodeId with role $role. Credentials in certs\nodes\$NodeId.cred (git-ignored)."
