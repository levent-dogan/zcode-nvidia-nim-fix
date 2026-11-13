[CmdletBinding()]
param(
    [switch]$DebugMode,

    [ValidateSet("diagnostic", "pass")]
    [string]$ToolCallTextMode = "diagnostic",

    [ValidateSet("Env", "Client", "Pool")]
    [string]$ApiKeyMode = "Env",

    [ValidateRange(1, 3600)]
    [int]$UpstreamTimeoutSeconds = 300,

    [string]$UpstreamProxy = "",

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
    [int]$Max5xxFailovers = 1,

    [switch]$PoolRetryLoop,

    [switch]$DisableModelDiscovery,

    [ValidateRange(30, 604800)]
    [int]$ModelRefreshSeconds = 21600,

    [string]$OpenCodeConfig = ""
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

$IniSettings = @{}
$IniPath = Join-Path $RepoRoot "proxy.ini"
if (Test-Path -LiteralPath $IniPath -PathType Leaf) {
    $SeenIniKeys = @{}
    $LineNumber = 0
    foreach ($Line in Get-Content -LiteralPath $IniPath) {
        $LineNumber++
        $TrimmedLine = $Line.Trim()
        if (
            [string]::IsNullOrWhiteSpace($TrimmedLine) -or
            $TrimmedLine.StartsWith("#") -or
            $TrimmedLine.StartsWith(";")
        ) {
            continue
        }
        if ($TrimmedLine -notmatch "^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
            Stop-WithMessage "Invalid proxy.ini syntax at line $LineNumber. Expected KEY = value."
        }
        $IniName = $Matches[1]
        $IniValue = $Matches[2].Trim()
        if (
            $IniValue.Length -ge 2 -and
            (($IniValue.StartsWith('"') -and $IniValue.EndsWith('"')) -or
             ($IniValue.StartsWith("'") -and $IniValue.EndsWith("'")))
        ) {
            $IniValue = $IniValue.Substring(1, $IniValue.Length - 2)
        }
        if ($SeenIniKeys.ContainsKey($IniName)) {
            Stop-WithMessage "Duplicate key '$IniName' in proxy.ini."
        }
        $SeenIniKeys[$IniName] = $true
        $IniSettings[$IniName] = $IniValue
    }
}

foreach ($NetworkName in @("Host", "Port")) {
    if (-not $IniSettings.ContainsKey($NetworkName)) {
        continue
    }
    $NetworkValue = $IniSettings[$NetworkName]
    if (
        $NetworkName -eq "Port" -and
        ($NetworkValue -notmatch "^\d{1,5}$" -or [int]$NetworkValue -gt 65535)
    ) {
        Stop-WithMessage "Invalid Port value in proxy.ini: '$NetworkValue'."
    }
    $NetworkEnvName = "NIM_PROXY_$NetworkName"
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($NetworkEnvName, "Process"))) {
        Set-Item -LiteralPath "Env:$NetworkEnvName" -Value $NetworkValue
    }
}
$IniSettings.Remove("Host")
$IniSettings.Remove("Port")

$IniBooleanKeys = @("DebugMode", "PoolRetryLoop", "DisableModelDiscovery")
$IniEnumValues = @{
    "ApiKeyMode" = @("Env", "Client", "Pool")
    "ToolCallTextMode" = @("diagnostic", "pass")
}
$IniRangeValues = @{
    "UpstreamTimeoutSeconds" = @(1, 3600)
    "MaxConcurrentPerKey" = @(1, 100)
    "MaxQueuePerKey" = @(1, 1000)
    "MaxTotalQueued" = @(1, 10000)
    "QueueWaitSeconds" = @(1, 3600)
    "RateLimitCooldownSeconds" = @(1, 3600)
    "Max5xxFailovers" = @(0, 10)
    "ModelRefreshSeconds" = @(30, 604800)
}
$IniStringKeys = @("UpstreamProxy", "EnvFile", "OpenCodeConfig")

foreach ($IniName in $IniSettings.Keys) {
    $IniValue = $IniSettings[$IniName]
    if ($IniBooleanKeys -contains $IniName) {
        if ($IniValue -notin @("on", "off", "true", "false", "1", "0")) {
            Stop-WithMessage "Invalid boolean value for '$IniName' in proxy.ini: '$IniValue'. Use on or off."
        }
        if (-not $PSBoundParameters.ContainsKey($IniName) -and $IniValue -in @("on", "true", "1")) {
            Set-Variable -Name $IniName -Value ([switch]$true)
        }
    } elseif ($IniEnumValues.ContainsKey($IniName)) {
        if ($IniEnumValues[$IniName] -notcontains $IniValue) {
            $AllowedValues = $IniEnumValues[$IniName] -join ", "
            Stop-WithMessage "Invalid value for '$IniName' in proxy.ini: '$IniValue'. Expected one of: $AllowedValues."
        }
        if (-not $PSBoundParameters.ContainsKey($IniName)) {
            Set-Variable -Name $IniName -Value $IniValue
        }
    } elseif ($IniRangeValues.ContainsKey($IniName)) {
        $ParsedInt = 0
        if (-not [int]::TryParse($IniValue, [ref]$ParsedInt)) {
            Stop-WithMessage "Invalid integer value for '$IniName' in proxy.ini: '$IniValue'."
        }
        if ($ParsedInt -lt $IniRangeValues[$IniName][0] -or $ParsedInt -gt $IniRangeValues[$IniName][1]) {
            $RangeBounds = "$($IniRangeValues[$IniName][0])-$($IniRangeValues[$IniName][1])"
            Stop-WithMessage "Value for '$IniName' in proxy.ini is out of range ($RangeBounds): $ParsedInt."
        }
        if (-not $PSBoundParameters.ContainsKey($IniName)) {
            Set-Variable -Name $IniName -Value $ParsedInt
        }
    } elseif ($IniStringKeys -contains $IniName) {
        if (-not $PSBoundParameters.ContainsKey($IniName)) {
            Set-Variable -Name $IniName -Value $IniValue
        }
    } else {
        Stop-WithMessage "Unsupported key '$IniName' in proxy.ini."
    }
}

if ($DisableModelDiscovery.IsPresent -and -not [string]::IsNullOrWhiteSpace($OpenCodeConfig)) {
    throw "OpenCode model sync cannot be used with -DisableModelDiscovery."
}
if ($PoolRetryLoop.IsPresent -and $ApiKeyMode -ne "Pool") {
    throw "-PoolRetryLoop requires -ApiKeyMode Pool."
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
    "18787"
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
if (-not [string]::IsNullOrWhiteSpace($UpstreamProxy)) {
    $PythonArgs += @("--upstream-proxy", $UpstreamProxy)
}
if ($DebugMode.IsPresent) {
    $PythonArgs += "--debug"
}
if ($PoolRetryLoop.IsPresent) {
    $PythonArgs += "--pool-retry-loop"
}
$PythonArgs += @("--model-refresh-seconds", $ModelRefreshSeconds)
if ($DisableModelDiscovery.IsPresent) {
    $PythonArgs += "--no-model-discovery"
}
if (-not [string]::IsNullOrWhiteSpace($OpenCodeConfig)) {
    $ResolvedOpenCodeConfig = if ([System.IO.Path]::IsPathRooted($OpenCodeConfig)) {
        $OpenCodeConfig
    } else {
        Join-Path $RepoRoot $OpenCodeConfig
    }
    $PythonArgs += @("--opencode-config", $ResolvedOpenCodeConfig)
}

Write-Host "Starting ZCode NVIDIA NIM proxy on http://${DisplayHost}:${DisplayPort}/v1"
if ($IniSettings.Count -gt 0) {
    Write-Host "Launcher settings loaded: proxy.ini"
}
Write-Host "Author: Levent Dogan" -ForegroundColor Cyan
Write-Host "Plain-text tool_call handling mode: $ToolCallTextMode"
Write-Host "API key mode: $NormalizedApiKeyMode"
Write-Host "Upstream timeout: $UpstreamTimeoutSeconds seconds"
if (-not [string]::IsNullOrWhiteSpace($UpstreamProxy)) {
    Write-Host "Upstream proxy: $UpstreamProxy"
}
Write-Host "Queue: $MaxConcurrentPerKey active per key, $MaxQueuePerKey waiting per key, $MaxTotalQueued waiting total"
if ($ApiKeyMode -eq "Pool") {
    Write-Host "NVIDIA key pool entries loaded: $($NumberedKeyNames.Count)"
}
if ($PoolRetryLoop.IsPresent) {
    Write-Host "Pool retry loop enabled while clients stay connected. Press Ctrl+C to stop the proxy."
}
if ($DebugMode.IsPresent) {
    Write-Host "Debug-safe logging enabled. API keys and full message content are not printed by the proxy."
}

$LocationPushed = $false
try {
    Push-Location -LiteralPath $RepoRoot
    $LocationPushed = $true
    python @PythonArgs
    exit $LASTEXITCODE
} finally {
    if ($LocationPushed) {
        Pop-Location
    }
    foreach ($Name in $ManagedEnvironmentNames) {
        $Original = $OriginalEnvironment[$Name]
        if ($Original.Exists) {
            Set-Item -LiteralPath "Env:$Name" -Value $Original.Value
        } else {
            Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        }
    }
}
