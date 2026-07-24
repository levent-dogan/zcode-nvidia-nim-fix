# zcode-nvidia-nim-fix

Windows-friendly local compatibility proxy for using NVIDIA NIM OpenAI-compatible chat models inside ZCode.

This project fixes the ZCode + NVIDIA NIM request failure:

```text
Validation: Unsupported parameter(s): `extra_body`
```

ZCode can send provider-extension fields such as `extra_body`. NVIDIA NIM rejects those fields at the top level of `/chat/completions` requests. This proxy runs locally, removes unsupported fields, and forwards a clean OpenAI-compatible request to NVIDIA NIM.

No NVIDIA API keys are stored in this repository. Do not commit real keys, `.env` files, screenshots with visible keys, or private provider names.

## Use Cases And Keywords

This repository is intended for users searching for:

- ZCode NVIDIA NIM compatibility fix
- ZCode `extra_body` unsupported parameter error
- NVIDIA NIM OpenAI-compatible local proxy
- `z-ai/glm-5.2` with ZCode
- GLM 5.2 NVIDIA NIM `/chat/completions`
- Windows PowerShell launcher for NVIDIA NIM proxy
- Multiple NVIDIA API keys with ZCode custom providers

## What This Proxy Does

- Listens locally at `http://127.0.0.1:8787/v1`.
- Accepts OpenAI-compatible `POST /v1/chat/completions` requests from ZCode.
- Forwards requests to `https://integrate.api.nvidia.com/v1/chat/completions`.
- Removes NVIDIA-unsupported top-level fields such as `extra_body`.
- Preserves standard OpenAI-compatible fields such as `model`, `messages`, `stream`, `tools`, and `tool_choice`.
- Supports one environment key, per-provider client keys, or a private round-robin key pool.
- Queues requests per NVIDIA key and applies bounded, status-aware failover in pool mode.
- Does not print API keys or full message content.

## Screenshot

The proxy running in `Client` API key mode with debug-safe logging:

![ZCode NVIDIA NIM proxy running in client API key mode](screenshot/screenshot_1.png)

ZCode custom provider configuration using the local proxy URL:

![ZCode custom provider configured with the local NVIDIA NIM proxy](screenshot/screenshot_2.png)

## Requirements

- Windows 10/11
- PowerShell 5.1 or PowerShell 7+
- Python 3.10 or newer
- One or more NVIDIA API keys from NVIDIA NIM
- ZCode custom provider access

Check Python:

```powershell
python --version
```

## Step 1: Open The Repository

Open PowerShell in the repository folder:

```powershell
cd C:\Path\To\zcode-nvidia-nim-fix
```

## Step 2: Create The Virtual Environment

Run once:

```powershell
python -m venv .venv
```

If PowerShell blocks script activation, allow it only for the current PowerShell process:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Upgrade pip:

```powershell
python -m pip install --upgrade pip
```

No third-party runtime package is required for the proxy itself. It uses Python standard library modules.

## Step 3: Choose API Key Mode

### Option A: Env Mode (One NVIDIA API Key)

Use this when all ZCode providers should share one NVIDIA API key.

```powershell
$env:NVIDIA_API_KEY="YOUR_NVIDIA_API_KEY"
.\run_proxy.ps1 -ApiKeyMode Env
```

In ZCode, the provider API key can be any placeholder because the proxy uses `NVIDIA_API_KEY`.
Requests are queued for this one key and processed one at a time by default.

### Option B: Client Mode (One Real Key Per ZCode Provider)

Use this when several ZCode providers should each use their own NVIDIA API key.

Start the proxy:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -UpstreamTimeoutSeconds 600
```

Then configure every ZCode provider with the same local base URL and its own NVIDIA key:

| ZCode field | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:8787/v1` |
| API format | `Chat completions (/chat/completions)` |
| API key | That provider's own NVIDIA API key |
| Model | Any NVIDIA NIM chat model ID available to your key |

Example six-provider layout:

| Provider | Base URL | API key field |
| --- | --- | --- |
| NVIDIA Key 1 | `http://127.0.0.1:8787/v1` | NVIDIA key #1 |
| NVIDIA Key 2 | `http://127.0.0.1:8787/v1` | NVIDIA key #2 |
| NVIDIA Key 3 | `http://127.0.0.1:8787/v1` | NVIDIA key #3 |
| NVIDIA Key 4 | `http://127.0.0.1:8787/v1` | NVIDIA key #4 |
| NVIDIA Key 5 | `http://127.0.0.1:8787/v1` | NVIDIA key #5 |
| NVIDIA Key 6 | `http://127.0.0.1:8787/v1` | NVIDIA key #6 |

In `Client` mode, the proxy forwards ZCode's incoming `Authorization: Bearer ...` token to NVIDIA NIM. Requests using the same key enter the same FIFO queue; requests using different keys can run in parallel. The key is never printed, and this mode cannot switch keys automatically because the proxy knows only the key supplied with that request.

You can point several ZCode projects or providers at the same local proxy URL while using different NVIDIA API keys in each provider. The proxy handles concurrent local requests with separate request threads. If several projects target the same NVIDIA model at the same time, NVIDIA can still return `429` or slow responses because model capacity, per-key limits, or account-level limits may still apply upstream.

For most multi-project setups, run one proxy in `Client` mode and reuse `http://127.0.0.1:8787/v1` in each project. Do not start several proxy processes on the same port. If you intentionally need separate proxy processes, assign a different `NIM_PROXY_PORT` to each process and use the matching Base URL in ZCode.

### Option C: Pool Mode (Recommended For One Local ZCode Key)

Use this when ZCode should contain one local proxy credential while the proxy privately manages all NVIDIA keys.

1. Create the local file:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Generate a local-only proxy secret:

   ```powershell
   ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
   ```

3. Open `.env` locally. Replace `NIM_PROXY_CLIENT_KEY` with the generated value and replace `NVIDIA_API_KEY_1` through `NVIDIA_API_KEY_6` with the six real NVIDIA keys.
4. Start the pool:

   ```powershell
   .\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
   ```

5. In ZCode, use the `NIM_PROXY_CLIENT_KEY` value as the provider API key. Never put one of the real NVIDIA keys in that ZCode provider.

The proxy selects healthy keys continuously in numeric order:

```text
NVIDIA_API_KEY_1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 1 -> 2 -> ...
```

The cycle does not stop after key 6. Busy, cooling-down, or quarantined keys are skipped. A cooled-down key automatically rejoins the cycle; a key rejected with `401` or `403` remains quarantined until the proxy restarts.

The `.env` parser accepts only `NIM_PROXY_CLIENT_KEY` and numbered `NVIDIA_API_KEY_n` entries. It does not execute the file, does not print values, rejects duplicate NVIDIA keys, and rejects a local proxy key that is also used as an NVIDIA key. `.env` is ignored by Git.

## Step 4: Configure ZCode

For each custom provider:

1. Open ZCode provider settings.
2. Add or edit a custom provider.
3. Set Base URL:

   ```text
   http://127.0.0.1:8787/v1
   ```

4. Set API format:

   ```text
   Chat completions (/chat/completions)
   ```

5. Set API key:
   - In `Env` mode: any placeholder.
   - In `Client` mode: the real NVIDIA API key for that provider.
   - In `Pool` mode: the local `NIM_PROXY_CLIENT_KEY` value from `.env`.
6. Add model IDs you want to use, for example:

   ```text
   z-ai/glm-5.2
   z-ai/glm-5.1
   moonshotai/kimi-k2.6
   deepseek-ai/deepseek-v4-pro
   qwen/qwen3-coder-480b-a35b-instruct
   nvidia/nemotron-3-ultra-550b-a55b
   ```

The proxy is not limited to GLM 5.2. It forwards the `model` value sent by ZCode. The selected model must be available through NVIDIA NIM and support `/chat/completions`.

## Step 5: Test The Proxy

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

Manual chat completion test in `Env` mode:

```powershell
$env:NVIDIA_API_KEY="YOUR_NVIDIA_API_KEY"
.\run_proxy.ps1 -DebugMode
```

Open a second PowerShell window:

```powershell
$body = @{
  model = "z-ai/glm-5.2"
  messages = @(
    @{
      role = "user"
      content = "Say hello in one sentence."
    }
  )
  stream = $false
  max_tokens = 128
  extra_body = @{
    chat_template_kwargs = @{
      enable_thinking = $false
    }
  }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8787/v1/chat/completions" `
  -Headers @{ Authorization = "Bearer placeholder" } `
  -ContentType "application/json" `
  -Body $body
```

Expected proxy log:

```text
Stripped unsupported NVIDIA NIM request keys: extra_body
```

In `Pool` mode, replace the test request's placeholder bearer value with the local `NIM_PROXY_CLIENT_KEY`, never a real NVIDIA key.

## Launcher Commands

Default one-key mode:

```powershell
$env:NVIDIA_API_KEY="YOUR_NVIDIA_API_KEY"
.\run_proxy.ps1
```

Default one-key mode with debug-safe logs:

```powershell
$env:NVIDIA_API_KEY="YOUR_NVIDIA_API_KEY"
.\run_proxy.ps1 -DebugMode
```

Multiple-key mode:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -DebugMode
```

Recommended client-key mode for normal daily use:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -UpstreamTimeoutSeconds 600
```

Recommended pool mode for six private NVIDIA keys:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

Pool mode while troubleshooting:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -DebugMode -UpstreamTimeoutSeconds 600
```

Use `-DebugMode` when you are checking stripped keys, HTTP status codes, timeout behavior, or ZCode provider problems. For normal daily use, leave `-DebugMode` off to keep the console quieter. API keys and full message content are not printed by the proxy in either mode.

Dedicated Windows debug launchers:

```bat
start_proxy_pool_debug.bat
start_proxy_client_debug.bat
```

- `start_proxy_pool_debug.bat` loads the private NVIDIA keys from `.env`. Configure ZCode with the local `NIM_PROXY_CLIENT_KEY`; the real NVIDIA keys remain local.
- `start_proxy_client_debug.bat` forwards the NVIDIA key supplied by each ZCode provider. This mode does not use automatic pool rotation or failover.
- Both launchers use port `8787` and a 600-second upstream timeout. Stop the active proxy with `Ctrl+C` before switching modes.

Pass raw upstream tool-call-looking text instead of readable diagnostics:

```powershell
.\run_proxy.ps1 -ToolCallTextMode pass -DebugMode
```

Batch wrapper:

```bat
start_proxy.bat
start_proxy.bat -ApiKeyMode Client -DebugMode
start_proxy.bat -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

Advanced queue and failover tuning:

```powershell
.\run_proxy.ps1 `
  -ApiKeyMode Pool `
  -UpstreamTimeoutSeconds 600 `
  -MaxConcurrentPerKey 1 `
  -MaxQueuePerKey 4 `
  -MaxTotalQueued 32 `
  -QueueWaitSeconds 180 `
  -RateLimitCooldownSeconds 60 `
  -Max5xxFailovers 1
```

These are the defaults except for the example's `600`-second upstream timeout. Increasing concurrency can increase NVIDIA rate-limit errors; keep `-MaxConcurrentPerKey 1` unless NVIDIA documents a higher safe limit for your account.

## Request Sanitizer

For NVIDIA NIM requests, the proxy keeps these top-level fields:

- `model`
- `messages`
- `temperature`
- `top_p`
- `max_tokens`
- `stream`
- `stream_options`
- `seed`
- `stop`
- `frequency_penalty`
- `presence_penalty`
- `tools`
- `tool_choice`
- `parallel_tool_calls`

It removes unsupported or provider-specific top-level fields such as:

- `extra_body`
- `extraBody`
- `chat_template_kwargs`
- `enable_thinking`
- `reasoning_effort`
- unknown provider-extension fields

`tools`, `tool_choice`, and `parallel_tool_calls` are preserved because they are standard OpenAI-compatible tool-calling fields.

Streaming responses are relayed incrementally. The proxy inspects only the first SSE event for an early plain-text tool-call marker, then forwards subsequent chunks without waiting for a large buffer to fill.

## Queue And Failover Behavior

| Condition | Behavior |
| --- | --- |
| Same key already active in `Env` or `Client` mode | Wait in that key's FIFO queue |
| Different healthy keys | May run in parallel |
| All pool keys busy | Wait in the process-wide FIFO pool queue |
| Local queue full | Return `429 proxy_queue_full` with `Retry-After: 5` |
| Queue wait exceeds the configured limit | Return `504 proxy_queue_timeout` |
| NVIDIA `408` or `429` in pool mode | Cool down that key using `Retry-After` or 60 seconds, then try another untried key |
| NVIDIA `401` or `403` in pool mode | Quarantine that key until restart, then try another untried key |
| NVIDIA `5xx` or transport failure in pool mode | Try at most one alternate key by default |
| NVIDIA `400`, `404`, or `422` | Return the response without switching keys |
| Streaming response has started | Never retry; relay or close that same response |

Each request tries any given key at most once. If bounded failover is exhausted, the final NVIDIA response is returned unchanged so ZCode sees the real upstream status.

Health endpoint:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health | ConvertTo-Json -Depth 5
```

Pool mode reports only aggregate counts such as total, available, cooling-down, quarantined, active, and queued keys. It never returns key values or fingerprints.

## GLM 5.2 Compatibility Note

NVIDIA's GLM 5.2 sample uses:

- `base_url = "https://integrate.api.nvidia.com/v1"`
- `model = "z-ai/glm-5.2"`
- `temperature = 1`
- `top_p = 1`
- `max_tokens = 16384`
- `seed = 42`
- `stream = true`

Those fields are preserved by the sanitizer. The NVIDIA sample does not use `extra_body`, which matches this project's fix.

## Tool-Call Diagnostic Behavior

Some NVIDIA NIM models may return text like this instead of real OpenAI-compatible `tool_calls` data:

```text
<tool_call>Read ...</tool_call>
```

The proxy does not execute or parse that text as a command. By default, it replaces the long raw markup with a readable diagnostic message. This keeps ZCode output understandable and avoids treating model-generated text as trusted tool execution.

If this happens, the request reached NVIDIA NIM, but the selected model or model profile may not support the structured tool-call format expected by ZCode.

## Troubleshooting HTTP 429

If the proxy log shows:

```text
"POST /v1/chat/completions HTTP/1.1" 429 -
```

that means the local proxy is running and NVIDIA NIM returned `429 Too Many Requests`. A successful model response would normally be `200`, but changing `429` to `200` in the proxy would hide the real upstream rate-limit or quota problem.

In debug mode, the proxy also logs a safe key fingerprint:

```text
NVIDIA NIM upstream response status=429 ... api_key_fingerprint='abc123def456' retry_after='30'
```

The fingerprint is a short SHA-256 prefix. It lets you see whether the same provider key or several different keys are hitting limits without printing the real NVIDIA API key.

Common causes:

- The proxy is running in `env` mode and all ZCode providers are sharing one NVIDIA API key.
- Too many requests are sent to the same NVIDIA key.
- The selected NVIDIA model has a per-key quota or concurrency limit.
- ZCode is retrying quickly after failed requests.

Check the proxy startup log:

```text
API key mode: env
```

If you have multiple NVIDIA API keys, stop the proxy with `Ctrl+C` and either restart in client mode:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -DebugMode
```

or configure `.env` and restart in pool mode:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -DebugMode -UpstreamTimeoutSeconds 600
```

Keep the same Base URL for all ZCode providers:

```text
http://127.0.0.1:8787/v1
```

In client mode, put a different NVIDIA API key in each provider's `API key` field. In pool mode, put the same local `NIM_PROXY_CLIENT_KEY` in every provider and let the proxy rotate the private key pool.

## Troubleshooting HTTP 504

If ZCode shows:

```text
Gateway Timeout
status=504
retryable=true
```

or the proxy log shows:

```text
NVIDIA NIM transport error ... error=TimeoutError
```

the local proxy sent the request to NVIDIA NIM, but NVIDIA did not start an HTTP response before the proxy timeout. This is different from the original `extra_body` validation error.

Common causes:

- NVIDIA NIM is slow or queued for the selected model.
- The prompt is large or the requested output is long.
- ZCode retried several long-running requests.
- The selected model is under load.

Recommended actions:

- Retry with a shorter prompt first.
- Try another NVIDIA NIM model.
- Keep `-ApiKeyMode Client` or `-ApiKeyMode Pool` enabled if you have multiple keys.
- Increase the upstream timeout when you expect long responses:

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -DebugMode -UpstreamTimeoutSeconds 600
```

For pool mode, use the same timeout with `-ApiKeyMode Pool`. A transport timeout can use one alternate key by default, but no retry occurs after a streaming response starts.

If ZCode has its own shorter client-side timeout, increasing the proxy timeout cannot fully solve that. In that case, use smaller prompts or a faster model.

## Troubleshooting Client Disconnect Logs

If the console shows a Windows client disconnect such as:

```text
ConnectionAbortedError: [WinError 10053]
```

ZCode closed the local HTTP connection before the proxy could send its final timeout/error response. This can happen after a long NVIDIA NIM wait. The proxy now suppresses the Python traceback and logs a short message instead:

```text
Client disconnected before JSON response could be sent
```

This is not an API key leak and usually does not mean the proxy crashed.

## Development Checks

Install developer tools if needed:

```powershell
python -m pip install pytest ruff mypy
```

Run tests:

```powershell
python -m pytest
```

Run lint and type checks:

```powershell
python -m ruff check .
python -m mypy nvidia_nim_proxy tests
```

## Configuration Reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `NVIDIA_API_KEY` | Required in `env` mode | NVIDIA NIM API key used for upstream requests |
| `NIM_PROXY_CLIENT_KEY` | Required in `pool` mode | Local bearer credential accepted from ZCode; never forwarded upstream |
| `NVIDIA_API_KEY_1...n` | At least one in `pool` mode | Private NVIDIA keys loaded in numeric round-robin order |
| `NIM_PROXY_HOST` | `127.0.0.1` | Local bind host |
| `NIM_PROXY_PORT` | `8787` | Local bind port |
| `NIM_PROXY_UPSTREAM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | OpenAI-compatible NVIDIA NIM base URL |
| `NIM_PROXY_TOOL_CALL_TEXT_MODE` | `diagnostic` | Use `diagnostic` for readable tool-call leak messages or `pass` for raw upstream output |
| `NIM_PROXY_API_KEY_MODE` | `env` | `env`, `client`, or `pool` |
| `NIM_PROXY_UPSTREAM_TIMEOUT_SECONDS` | `300` | Seconds to wait for NVIDIA NIM to start responding before returning `504` |
| `NIM_PROXY_MAX_CONCURRENT_PER_KEY` | `1` | Maximum active upstream requests per NVIDIA key |
| `NIM_PROXY_MAX_QUEUE_PER_KEY` | `4` | Per-key waiting limit in env/client mode |
| `NIM_PROXY_MAX_TOTAL_QUEUED` | `32` | Process-wide waiting limit |
| `NIM_PROXY_QUEUE_WAIT_SECONDS` | `180` | Maximum queue wait before local `504` |
| `NIM_PROXY_RATE_LIMIT_COOLDOWN_SECONDS` | `60` | Fallback cooldown when NVIDIA omits `Retry-After` |
| `NIM_PROXY_MAX_5XX_FAILOVERS` | `1` | Alternate keys permitted for a `5xx` or transport failure |

## Security And Privacy

- Do not commit NVIDIA API keys.
- Do not commit `.env` files.
- Do not commit screenshots that show visible keys.
- `.venv/`, `.env`, `.env.*`, cache folders, and package build output are ignored by `.gitignore`.
- The proxy binds to `127.0.0.1` by default.
- Non-loopback bindings are rejected unless `--allow-remote` is explicitly supplied.
- Remote binding is never allowed in `env` API key mode because that would expose the environment-backed NVIDIA key through a network-accessible proxy.
- Logs show stripped key names only, not secrets or full message content.
- In `Client` mode, incoming ZCode bearer tokens are forwarded to NVIDIA NIM but never printed.
- In `Pool` mode, ZCode's local bearer token is validated with a constant-time comparison and is never forwarded to NVIDIA.
- Restrict `.env` so only your Windows account can read it, and rotate the local proxy key if it appears in a screenshot or log.

Before publishing, check:

```powershell
git status --short
rg -n "nvapi-|NVIDIA_API_KEY=|Bearer " .
```

The search may find documentation placeholders such as `NVIDIA_API_KEY`; it should not find real keys.

## License

Released under the MIT License. See [LICENSE](LICENSE).

## Roadmap

- Add integration tests with a mock upstream streaming server.
- Add optional allowlist extension by environment variable for future NVIDIA-supported fields.
- Add a Windows service wrapper for persistent local use.
- Add optional privacy-preserving metrics export without logging prompt content.

## Versioning

This project follows semantic versioning.

See [CHANGELOG.md](CHANGELOG.md) for release history.

Current version: `0.2.1`.
