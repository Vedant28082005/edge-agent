@echo off
rem Double-click: stops running sims, then starts every saved simulated node in the background.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-sims.ps1" -Mode "%~1"
set RC=%ERRORLEVEL%
if /i not "%~1"=="auto" pause
exit /b %RC%
