[CmdletBinding()]
param(
    [switch]$DebugMode,

    [ValidateSet("diagnostic", "pass")]
    [string]$ToolCallTextMode = "diagnostic",

    [ValidateSet("Env", "Client")]
    [string]$ApiKeyMode = "Env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ActivateScript = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"

function Stop-WithMessage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-Error $Message
    exit 1
}

if (-not (Test-Path -LiteralPath $ActivateScript -PathType Leaf)) {
    Stop-WithMessage "Missing virtual environment activation script: $ActivateScript. Create it with: python -m venv .venv"
}

. $ActivateScript

if ($ApiKeyMode -eq "Env" -and [string]::IsNullOrWhiteSpace($env:NVIDIA_API_KEY)) {
    Stop-WithMessage "NVIDIA_API_KEY is not set. Set it for this PowerShell session with: `$env:NVIDIA_API_KEY='YOUR_KEY'"
}

$NormalizedApiKeyMode = $ApiKeyMode.ToLowerInvariant()
$PythonArgs = @(
    "-m",
    "nvidia_nim_proxy.server",
    "--tool-call-text-mode",
    $ToolCallTextMode,
    "--api-key-mode",
    $NormalizedApiKeyMode
)
if ($DebugMode.IsPresent) {
    $PythonArgs += "--debug"
}

Write-Host "Starting ZCode NVIDIA NIM proxy on http://127.0.0.1:8787/v1"
Write-Host "Author: Levent Dogan"
Write-Host "Plain-text tool_call handling mode: $ToolCallTextMode"
Write-Host "API key mode: $NormalizedApiKeyMode"
if ($DebugMode.IsPresent) {
    Write-Host "Debug-safe logging enabled. API keys and full message content are not printed by the proxy."
}

python @PythonArgs
