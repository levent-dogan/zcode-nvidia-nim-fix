@echo off
setlocal DisableDelayedExpansion

pushd "%~dp0"
if errorlevel 1 exit /b 1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_proxy.ps1" -ApiKeyMode Pool -PoolRetryLoop -DebugMode -UpstreamTimeoutSeconds 120
set "ProxyExitCode=%ERRORLEVEL%"

popd
exit /b %ProxyExitCode%
