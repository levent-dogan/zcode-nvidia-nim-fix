@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_proxy.ps1" -ApiKeyMode Client -DebugMode -UpstreamTimeoutSeconds 600

exit /b %ERRORLEVEL%
