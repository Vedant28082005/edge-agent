<#
.SYNOPSIS
  Starts the saved sims at Windows login: a shortcut to start-sims.bat in your Startup folder (no admin needed).
  Remove it with uninstall-autostart.ps1.
#>
$ErrorActionPreference = 'Stop'
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Edge sim nodes.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = Join-Path $PSScriptRoot 'start-sims.bat'
$s.Arguments = 'auto'
$s.WorkingDirectory = $PSScriptRoot
$s.WindowStyle = 7   # minimized
$s.Description = 'Start the simulated edge nodes'
$s.Save()
Write-Host "Installed: $lnk"
Write-Host 'The sims start at login. The PC must be on the same LAN as the Pi; until the broker is reachable they keep retrying.'
