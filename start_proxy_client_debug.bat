@echo off
setlocal DisableDelayedExpansion

pushd "%~dp0"
if errorlevel 1 exit /b 1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_proxy.ps1" -ApiKeyMode Client -DebugMode -UpstreamTimeoutSeconds 600 -OpenCodeConfig "%USERPROFILE%\.config\opencode\opencode.json"
set "ProxyExitCode=%ERRORLEVEL%"

popd
exit /b %ProxyExitCode%
