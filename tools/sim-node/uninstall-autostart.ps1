<#
.SYNOPSIS
  Removes the login shortcut created by install-autostart.ps1.
#>
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Edge sim nodes.lnk'
if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "Removed: $lnk" } else { Write-Host 'No autostart shortcut installed.' }
