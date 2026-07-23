[CmdletBinding()]
param(
    [switch]$DebugMode,

    [ValidateSet("diagnostic", "pass")]
    [string]$ToolCallTextMode = "diagnostic",

    [ValidateSet("Env", "Client", "Pool")]
    [string]$ApiKeyMode = "Env",

    [ValidateRange(1, 3600)]
    [int]$UpstreamTimeoutSeconds = 300,

    [string]$EnvFile = "",

    [ValidateRange(1, 100)]
    [int]$MaxConcurrentPerKey = 1,

    [ValidateRange(1, 1000)]
    [int]$MaxQueuePerKey = 4,

    [ValidateRange(1, 10000)]
    [int]$MaxTotalQueued = 32,

    [ValidateRange(1, 3600)]
    [int]$QueueWaitSeconds = 180,

    [ValidateRange(1, 3600)]
    [int]$RateLimitCooldownSeconds = 60,

    [ValidateRange(0, 10)]
    [int]$Max5xxFailovers = 1
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

function Read-ProxyEnvironmentFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Stop-WithMessage "Pool environment file not found: $Path. Create it from .env.example."
    }

    $AllowedNamePattern = "^(NIM_PROXY_CLIENT_KEY|NVIDIA_API_KEY_[1-9][0-9]*)$"
    $Values = @{}
    $SeenNames = @{}
    $LineNumber = 0

    foreach ($Line in Get-Content -LiteralPath $Path) {
        $LineNumber++
        $TrimmedLine = $Line.Trim()
        if ([string]::IsNullOrWhiteSpace($TrimmedLine) -or $TrimmedLine.StartsWith("#")) {
            continue
        }

        if ($TrimmedLine -notmatch "^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
            Stop-WithMessage "Invalid .env syntax at line $LineNumber. Expected NAME=value."
        }

        $Name = $Matches[1]
        $Value = $Matches[2].Trim()
        if ($Name -notmatch $AllowedNamePattern) {
            Stop-WithMessage "Unsupported variable '$Name' in the pool environment file."
        }
        if ($SeenNames.ContainsKey($Name)) {
            Stop-WithMessage "Duplicate variable '$Name' in the pool environment file."
        }
        if (
            $Value.Length -ge 2 -and
            (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
             ($Value.StartsWith("'") -and $Value.EndsWith("'")))
        ) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        if ([string]::IsNullOrWhiteSpace($Value)) {
            Stop-WithMessage "Variable '$Name' in the pool environment file is empty."
        }

        $SeenNames[$Name] = $true
        $Values[$Name] = $Value
    }

    return $Values
}

if (-not (Test-Path -LiteralPath $ActivateScript -PathType Leaf)) {
    Stop-WithMessage "Missing virtual environment activation script: $ActivateScript. Create it with: python -m venv .venv"
}

. $ActivateScript

$ManagedEnvironmentNames = @()
$OriginalEnvironment = @{}
if ($ApiKeyMode -eq "Pool") {
    $ResolvedEnvFile = if ([string]::IsNullOrWhiteSpace($EnvFile)) {
        Join-Path $RepoRoot ".env"
    } elseif ([System.IO.Path]::IsPathRooted($EnvFile)) {
        $EnvFile
    } else {
        Join-Path $RepoRoot $EnvFile
    }

    $PoolEnvironment = Read-ProxyEnvironmentFile -Path $ResolvedEnvFile
    if (-not $PoolEnvironment.ContainsKey("NIM_PROXY_CLIENT_KEY")) {
        Stop-WithMessage "NIM_PROXY_CLIENT_KEY is required in the pool environment file."
    }
    $NumberedKeyNames = @(
        $PoolEnvironment.Keys |
            Where-Object { $_ -match "^NVIDIA_API_KEY_[1-9][0-9]*$" }
    )
    if ($NumberedKeyNames.Count -eq 0) {
        Stop-WithMessage "At least one numbered NVIDIA_API_KEY_n value is required in pool mode."
    }
    foreach ($Name in $NumberedKeyNames) {
        if ($PoolEnvironment[$Name] -ceq $PoolEnvironment["NIM_PROXY_CLIENT_KEY"]) {
            Stop-WithMessage "NIM_PROXY_CLIENT_KEY must be different from every NVIDIA API key."
        }
    }

    $ExistingNumberedKeyNames = @(
        Get-ChildItem Env: |
            Where-Object { $_.Name -match "^NVIDIA_API_KEY_[1-9][0-9]*$" } |
            ForEach-Object { $_.Name }
    )
    $ManagedEnvironmentNames = @(
        @("NIM_PROXY_CLIENT_KEY") +
        $ExistingNumberedKeyNames +
        @($PoolEnvironment.Keys) |
            Sort-Object -Unique
    )

    foreach ($Name in $ManagedEnvironmentNames) {
        $ExistingValue = [Environment]::GetEnvironmentVariable($Name, "Process")
        $OriginalEnvironment[$Name] = @{
            Exists = $null -ne $ExistingValue
            Value = $ExistingValue
        }
        if ($PoolEnvironment.ContainsKey($Name)) {
            Set-Item -LiteralPath "Env:$Name" -Value $PoolEnvironment[$Name]
        } else {
            Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        }
    }
} elseif ($ApiKeyMode -eq "Env" -and [string]::IsNullOrWhiteSpace($env:NVIDIA_API_KEY)) {
    Stop-WithMessage "NVIDIA_API_KEY is not set. Set it for this PowerShell session with: `$env:NVIDIA_API_KEY='YOUR_KEY'"
}

$NormalizedApiKeyMode = $ApiKeyMode.ToLowerInvariant()
$DisplayHost = if ([string]::IsNullOrWhiteSpace($env:NIM_PROXY_HOST)) {
    "127.0.0.1"
} else {
    $env:NIM_PROXY_HOST
}
$DisplayPort = if ([string]::IsNullOrWhiteSpace($env:NIM_PROXY_PORT)) {
    "8787"
} else {
    $env:NIM_PROXY_PORT
}
$PythonArgs = @(
    "-m",
    "nvidia_nim_proxy.server",
    "--tool-call-text-mode",
    $ToolCallTextMode,
    "--api-key-mode",
    $NormalizedApiKeyMode,
    "--upstream-timeout-seconds",
    $UpstreamTimeoutSeconds,
    "--max-concurrent-per-key",
    $MaxConcurrentPerKey,
    "--max-queue-per-key",
    $MaxQueuePerKey,
    "--max-total-queued",
    $MaxTotalQueued,
    "--queue-wait-seconds",
    $QueueWaitSeconds,
    "--rate-limit-cooldown-seconds",
    $RateLimitCooldownSeconds,
    "--max-5xx-failovers",
    $Max5xxFailovers
)
if ($DebugMode.IsPresent) {
    $PythonArgs += "--debug"
}

Write-Host "Starting ZCode NVIDIA NIM proxy on http://${DisplayHost}:${DisplayPort}/v1"
Write-Host "Author: Levent Dogan" -ForegroundColor Cyan
Write-Host "Plain-text tool_call handling mode: $ToolCallTextMode"
Write-Host "API key mode: $NormalizedApiKeyMode"
Write-Host "Upstream timeout: $UpstreamTimeoutSeconds seconds"
Write-Host "Queue: $MaxConcurrentPerKey active per key, $MaxQueuePerKey waiting per key, $MaxTotalQueued waiting total"
if ($ApiKeyMode -eq "Pool") {
    Write-Host "NVIDIA key pool entries loaded: $($NumberedKeyNames.Count)"
}
if ($DebugMode.IsPresent) {
    Write-Host "Debug-safe logging enabled. API keys and full message content are not printed by the proxy."
}

try {
    python @PythonArgs
    exit $LASTEXITCODE
} finally {
    foreach ($Name in $ManagedEnvironmentNames) {
        $Original = $OriginalEnvironment[$Name]
        if ($Original.Exists) {
            Set-Item -LiteralPath "Env:$Name" -Value $Original.Value
        } else {
            Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        }
    }
}
