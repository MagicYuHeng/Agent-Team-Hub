@echo off
setlocal
set "PS=powershell"
where pwsh >nul 2>&1
if %errorlevel%==0 set "PS=pwsh"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Hub.ps1" %*
exit /b %errorlevel%
