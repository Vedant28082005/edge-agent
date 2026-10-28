@echo off
rem Double-click: stops all simulated nodes started by start-sims.bat / start-room.ps1.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-sims.ps1"
set RC=%ERRORLEVEL%
pause
exit /b %RC%
