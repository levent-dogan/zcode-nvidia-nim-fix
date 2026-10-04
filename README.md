# NVIDIA NIM Local Proxy for ZCode and OpenCode

`zcode-nvidia-nim-fix` is a local OpenAI-compatible Chat Completions proxy for NVIDIA NIM. It started as a workaround for ZCode sending a literal `extra_body` field, which NVIDIA rejects. It now supports model-specific request mapping, a private API-key pool, bounded queues, streaming, a cached model catalog, and optional OpenCode model synchronization. The package and repository names retain the original ZCode name; the proxy also works with other compatible clients.

**Current example model:** `z-ai/glm-5.3`. The proxy is not tied to this model. Check [NVIDIA's model catalog](https://integrate.api.nvidia.com/v1/models) and the chosen model's Chat Completions documentation before adding an ID. A catalog entry alone does not guarantee chat support or access for your key. GLM 5.2 remains supported where NVIDIA offers it, but it is now a historical example of the original `extra_body` error.

## At a Glance

| Item | Value |
| --- | --- |
| Local base URL | `http://127.0.0.1:18787/v1` |
| Chat endpoint | `POST /v1/chat/completions` |
| Cached model list | `GET /v1/models` |
| Health | `GET /health` |
| NVIDIA upstream | `https://integrate.api.nvidia.com/v1` |
| Windows launcher | `run_proxy.ps1` |

The proxy removes unsupported request fields, preserves messages and structured tool calls, and forwards JSON or SSE streaming responses. It does not implement the Responses, Embeddings, image, audio, or file APIs. It does not grant a larger context window or guarantee that a model will produce valid tool calls.

## Requirements and Installation

- Windows 10/11, PowerShell 5.1 or 7+, and Python 3.10+.
- At least one NVIDIA API key for inference.
- ZCode, OpenCode, or another client that supports a custom OpenAI-compatible **Chat Completions** base URL.

Open PowerShell in the repository directory and set up Python once:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

The proxy has no third-party runtime dependencies. `run_proxy.ps1` activates `.venv` automatically. If PowerShell blocks local scripts, run `Set-ExecutionPolicy -Scope Process Bypass` in that terminal before starting the launcher.

## Choose an API-Key Mode

Run **one** proxy process on port `18787` at a time. The modes use different credentials:

| Mode | What the client sends to the proxy | What NVIDIA receives | Key rotation |
| --- | --- | --- | --- |
| `Pool` | One private local `NIM_PROXY_CLIENT_KEY` | A key selected from private `NVIDIA_API_KEY_n` entries | Yes, among healthy keys |
| `Client` | That client's own real NVIDIA key | The same real key | No |
| `Env` | Any nonempty placeholder | One `NVIDIA_API_KEY` from the proxy process | No |

### Pool: One Local Credential, Multiple NVIDIA Keys

Recommended when several local projects should share a private key pool. Copy the safe template, then edit **only your local `.env`**:

```powershell
Copy-Item .env.example .env
```

Replace its seven `EXAMPLE_INVALID_...` values with a unique local `NIM_PROXY_CLIENT_KEY` and up to six real `NVIDIA_API_KEY_1` through `NVIDIA_API_KEY_6` values. The template values are randomly generated **invalid examples**, not usable credentials. To generate a local proxy credential, use:

```powershell
([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
```

Keep the generated value private. The local key must differ from every NVIDIA key. The `.env` parser accepts only the local key and numbered NVIDIA keys. `.env` is Git-ignored and must never be committed.

Start the pool:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

In the IDE, use the **local** `NIM_PROXY_CLIENT_KEY` as the provider API key. The proxy cycles through eligible keys in numeric order, returning to key 1 after key 6. Busy or cooling keys are skipped; `401`/`403` keys are quarantined until restart. A pool is useful for scheduling requests, but cannot bypass NVIDIA's per-account or per-model limits.

### Client: A Separate Real Key per Provider

```powershell
.\run_proxy.ps1 -ApiKeyMode Client -UpstreamTimeoutSeconds 600
```

Give each IDE provider the local base URL and **its own real NVIDIA key**. Requests using one key share its FIFO queue. Different keys may run concurrently. The proxy cannot rotate a Client-mode request to another key.

### Env: One Real Key in the Proxy Process

```powershell
$env:NVIDIA_API_KEY = 'REPLACE_LOCALLY_WITH_YOUR_KEY'
.\run_proxy.ps1 -ApiKeyMode Env
```

The client can use any nonempty placeholder as its API key. The proxy uses `NVIDIA_API_KEY` for every upstream request. Do not copy the placeholder into a published configuration.

## Configure ZCode or Another IDE

Create a custom **OpenAI-compatible / Chat Completions** provider:

| Client setting | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:18787/v1` |
| Endpoint | `/chat/completions` |
| API key | Credential for the mode selected above |
| Model ID | For example, `z-ai/glm-5.3` or another supported NVIDIA chat model |

Some clients append `/v1` themselves. In that case configure `http://127.0.0.1:18787`; the final path arriving at the proxy must be `/v1/chat/completions`. The same local URL can serve several IDE projects. If you deliberately run a second proxy, use a different `NIM_PROXY_PORT` and matching client URL.

ZCode may offer a context-window and maximum-output setting. Use limits that the **hosted NVIDIA endpoint** actually accepts. A model card's architectural limit is not proof of the hosted limit. Input plus requested output can exceed NVIDIA's allowance even when the client shows a larger context value. The proxy does not compact conversations or alter token budgets.

## Configure OpenCode GUI

OpenCode supports custom providers through JSON configuration. This repository includes a credential-free [example](examples/opencode.json) using `@ai-sdk/openai-compatible`. The global Windows file is `%USERPROFILE%\.config\opencode\opencode.json`; OpenCode may also load `opencode.jsonc`, project settings, or `OPENCODE_CONFIG` overrides. See the [OpenCode configuration guide](https://opencode.ai/docs/config/) for precedence.

### Local Pool Provider

The example defines `nim-local` at `http://127.0.0.1:18787/v1`. Merge its `provider.nim-local` object into your existing configuration rather than replacing other providers. In OpenCode, connect provider ID `nim-local` and use your **local** `NIM_PROXY_CLIENT_KEY` from `.env`. Select a model from the OpenCode model picker. Start the Pool-mode proxy before chatting.

The proxy can add current, documented Chat Completions models to `nim-local` when they appear in NVIDIA's live public catalog. Close OpenCode for the initial sync, then start:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -DebugMode -UpstreamTimeoutSeconds 600 `
  -OpenCodeConfig "$HOME\.config\opencode\opencode.json"
```

Wait for `Model catalog refreshed` and `OpenCode model sync complete`, then reopen OpenCode. Sync preserves selected model, credentials, and non-DeepSeek manual entries; it removes obsolete DeepSeek entries and adds the current V4.1 Flash ID under `nim-local` and any existing `nvidia_nim_1` through `nvidia_nim_6` providers that point to the official NVIDIA endpoint. Other providers are untouched. It only uses a successful public catalog refresh, not a stale cache. The live `/v1/models` feed includes embedding, safety, and other non-chat models, so the OpenCode list is intentionally limited to documented chat models; it is not every catalog ID. It accepts **strict JSON** at the selected path; JSONC, malformed files, duplicate properties, symlinks, or a provider with a conflicting endpoint are rejected. Before changing an existing file, it stores a private backup under `%LOCALAPPDATA%\zcode-nvidia-nim-fix\opencode-backups`. Keep those backups private because your existing config may contain credentials.

### Six Direct NVIDIA Providers

You may additionally configure `nvidia_nim_1` through `nvidia_nim_6` as separate OpenCode providers, each with `https://integrate.api.nvidia.com/v1` and its own NVIDIA key stored in OpenCode's local credential store. These **bypass the proxy**, including its sanitizer, pool, queue, and default reasoning mapping. Model sync also updates these six existing providers when their endpoint matches exactly, but it does not create them or change their credentials. Do not put real keys in JSON examples or the repository.

### Model Context Limits and Automatic Compaction

Reviewed **2026-10-04**. Previously, model sync assigned every model a generic
`131072` context and `16384` output budget and skipped existing entries. This
made OpenCode compact much earlier than documented for larger-context models.
The [limit registry](nvidia_nim_proxy/model_limits.py) now uses exact model IDs:

| Model ID | Configured context (tokens) | Configured output budget | NVIDIA source |
| --- | ---: | ---: | --- |
| `z-ai/glm-5.3` | 1,048,576 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/z-ai-glm-5-3) |
| `z-ai/glm-5.3-flash` | 1,048,576 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/z-ai-glm-5-3-flash) |
| `moonshotai/kimi-k3` | 1,048,576 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k3) |
| `deepseek-ai/deepseek-v4.1-flash` | 1,048,576 | 16,384 | [Model card](https://build.nvidia.com/deepseek-ai/deepseek-v4.1-flash/modelcard) |
| `moonshotai/kimi-k2.6` | 262,144 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k2-6) |
| `google/gemma-4-31b-it` | 262,144 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it) |
| `poolside/laguna-xs-2.1` | 262,144 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/poolside-laguna-xs-2-1) |
| `nvidia/nemotron-3-super-120b-a12b` | 1,048,576 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b) |
| `nvidia/nemotron-3-ultra-550b-a55b` | 1,048,576 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-ultra-550b-a55b) |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | 1,048,576 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b), [NVIDIA's exact 1M recipe](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16/blob/main/README.md) |
| `meta/muse-glimmer-30b` | 131,072 | 16,384 | [Model card](https://docs.api.nvidia.com/nim/reference/meta-muse-glimmer-30b) |
| `openai/gpt-oss-20b` | 131,072 | 4,096 | Hosted validation reported 131,072; [output schema](https://docs.api.nvidia.com/nim/reference/openai-gpt-oss-20b-infer) |

**Evidence boundary:** most context values above are documented model capacities,
not independently verified maximum-length hosted requests. Small synthetic
validation requests did not confirm the full window for GLM, Kimi or DeepSeek:
several timed out, Kimi K2.6 returned `404`, and some endpoints accepted streaming
headers without revealing their context ceiling. GPT-OSS returned an explicit
131,072-token ceiling. A `200` header or a `max_tokens` validation cap is **not**
proof that a long input fits. No full-window inference/load test was run.
If NVIDIA returns a smaller deployment-specific context limit, its error takes
precedence: set that verified value manually for that provider/model. This
registry does not unlock a larger server-side deployment or apply 1M to GLM 5.2.

`output` is an operating budget, **not** an assertion of the model's maximum
output capacity or its reasoning effort. We retain 16,384 rather than reserving
the entire context for output; GPT-OSS uses its documented 4,096 hosted cap.
Reasoning tokens can share the generation budget. Input, tool schemas, output
reservation and client safety margins all matter. In [OpenCode
1.15.13](https://github.com/anomalyco/opencode/blob/v1.15.13/packages/opencode/src/session/overflow.ts),
the normal compaction threshold subtracts the effective output reservation from
context; an explicit `limit.input` introduces a separate input ceiling. Exact
behavior is version-dependent. Keep automatic compaction enabled for safety.

On a successful live-catalog refresh, sync fills missing limits and migrates only
the exact old `{ "context": 131072, "output": 16384 }` template for known models.
Other explicit limits, including an explicit `input` cap, are preserved. Existing
model names, effort options, credentials and other providers are unchanged.
If you intentionally used that exact old pair, replace it with your preferred
non-template limit before sync. Unknown/new model IDs never inherit a guessed 1M.

To apply: close OpenCode, run the Pool launch command above, wait for successful
model sync, then reopen OpenCode. **Disconnect is unnecessary.** If the displayed
limit remains stale, check the selected provider ID and overrides in
`opencode.jsonc`, project config or `OPENCODE_CONFIG`; edit the effective model's
`limit.context` and `limit.output`, not the credential file. Start a short test
conversation before resuming a large coding session. Changes to displayed limits
cannot restore detail already removed by an earlier compaction.

### Add a Model Without Disconnecting

1. Confirm the **exact** model ID in the [public catalog](https://integrate.api.nvidia.com/v1/models), then verify that it supports Chat Completions and the capabilities your client needs.
2. Close OpenCode. Back up the global configuration outside the repository:

   ```powershell
   $configPath = Join-Path $HOME '.config\opencode\opencode.json'
   $backupPath = "$configPath.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
   Copy-Item -LiteralPath $configPath -Destination $backupPath
   ```

3. Edit `provider.<provider-id>.models` in that file. Keep existing entries, separate siblings with commas, and add the exact ID. For example, **inside** a `models` object:

   ```json
   {
     "existing/model-id": { "name": "Existing model" },
     "vendor/new-chat-model-id": {
       "name": "New chat model",
       "limit": { "context": 131072, "output": 16384 }
     }
   }
   ```

   Replace the example limits with values supported by the endpoint. Add `reasoning`, `tool_call`, or `interleaved` settings only when the model supports them. Add the model separately under `nim-local` and/or whichever direct providers should display it; no disconnect is needed.
4. Validate the file, reopen OpenCode, and choose the new model:

   ```powershell
   Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json | Out-Null
   opencode models nim-local --pure
   ```

   Substitute `nvidia_nim_1` or another provider ID in the last command as needed. Seeing a model in the list confirms configuration loading, **not** successful inference. Test with a short prompt.

If a provider disappears, inspect `disabled_providers` and any duplicate provider definition in `opencode.jsonc`, project config, or other OpenCode configuration sources. OpenCode can disable a provider after **Disconnect** even though its model entries remain in `opencode.json`. The local credential file `%USERPROFILE%\.local\share\opencode\auth.json` is for authentication; model entries do not belong there.

## Supported Model Behavior

The proxy recognizes exact IDs in [the reasoning profile registry](nvidia_nim_proxy/model_profiles.py). A newly published catalog ID does not automatically gain a new reasoning mapping. These rules apply to **requests through the proxy**, including models manually added to the IDE:

| Model ID | No effort selected in client | Explicit supported values |
| --- | --- | --- |
| `moonshotai/kimi-k3` | Sends top-level `reasoning_effort: "max"` | `low`, `high`, `max` |
| `z-ai/glm-5.3`, `z-ai/glm-5.3-flash` | Uses NVIDIA's documented native `max` default; sends no unverified wire override | None enabled for hosted API |
| `deepseek-ai/deepseek-v4.1-flash` | Uses hosted default; NVIDIA's model page does not document a hosted `max` request field, so the proxy does not invent one | No verified hosted override |
| Retired DeepSeek V4 Pro/Flash IDs, if manually requested | Legacy mapping remains for compatibility, but these IDs are not synced into OpenCode and may be unavailable upstream | `none`, `high`, `max` |
| `openai/gpt-oss-20b`, `openai/gpt-oss-120b` | Uses provider default; `max` is not a documented value | `low`, `medium`, `high` |
| Other models, including GLM 5.2 | Uses provider default; strips unknown effort fields | No guessed override |

For Kimi K3, the proxy also removes sampling fields NVIDIA documents as fixed. An explicit valid effort takes precedence over a profile default. Missing or unsupported values fall back to that profile's default. It never inserts a textual `/max` command into your prompt. `max` effort can increase latency; it does not change `max_tokens` or context length.

NVIDIA describes the GLM 5.3 model's default as `max`, but its [hosted request documentation](https://docs.api.nvidia.com/nim/reference/z-ai-glm-5-3-infer) has not been verified here to accept a per-request effort override. The proxy leaves that native default in place. DeepSeek V4.1 Flash's [model page](https://build.nvidia.com/deepseek-ai/deepseek-v4.1-flash) discusses high-effort evaluation, but its hosted example does not specify an effort parameter. Until NVIDIA publishes or a live test confirms the hosted wire format, selecting `Max` in OpenCode cannot be guaranteed for that model. Other models without a verified `max` capability use their provider default. References: [GLM 5.3 model card](https://build.nvidia.com/z-ai/glm-5-3/modelcard), [Kimi K3 API](https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k3-infer), and [NVIDIA model catalog](https://build.nvidia.com/models). A configured profile does not make a retired or inaccessible endpoint work.

## Model Catalog and Proxy API

When the upstream is NVIDIA's public endpoint, a background worker fetches `/v1/models` on startup and refreshes every six hours. It sends no API key, prompt, or project file. The last good public list is cached under `%LOCALAPPDATA%\zcode-nvidia-nim-fix\models.json`. A failed refresh retains the last good list and retries with bounded backoff. Discovery never blocks chat and does not restrict which model ID a chat request can use.

```powershell
(Invoke-RestMethod http://127.0.0.1:18787/health).model_catalog
```

Status can be `loading`, `ready`, `stale`, `unavailable`, or `disabled`. Before the first successful refresh, `GET /v1/models` returns `503 model_catalog_unavailable`; chat remains available. Pool mode requires the local bearer key for `/v1/models`. Client mode requires a nonempty bearer value, but the public catalog lookup does not verify it with NVIDIA. Env mode permits a local unauthenticated lookup. A custom upstream or `-DisableModelDiscovery` disables public catalog discovery. The public list can include non-chat models and contains no capability or per-key entitlement details.

## Test a Request

Start the proxy in your chosen mode in one terminal. In another terminal, check health and send a small chat request. The example uses a deliberately invalid placeholder and therefore requires replacement with your **local pool key** in Pool mode or your **NVIDIA key** in Client mode. In Env mode, any nonempty placeholder works.

```powershell
Invoke-RestMethod http://127.0.0.1:18787/health

$body = @{
  model = 'z-ai/glm-5.3'
  messages = @(@{ role = 'user'; content = 'Say hello in one sentence.' })
  stream = $false
  max_tokens = 128
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:18787/v1/chat/completions' `
  -Headers @{ Authorization = 'Bearer REPLACE_LOCALLY_WITH_MODE_CREDENTIAL' } `
  -ContentType 'application/json' -Body $body
```

The original ZCode failure was a request containing literal `extra_body`. The proxy drops that wrapper; only documented model-specific options may be mapped out of it. The GLM 5.2 case is retained in [the sanitizer tests](tests/test_integration.py). Do not send `extra_body` in a direct NVIDIA request.

The screenshot below shows an older GLM 5.2 session with `extra_body` stripped. The current example above uses GLM 5.3.

![Historical proxy console example](screenshot/screenshot_1.png)

The local ZCode base URL is shown here; the screenshot's masked API-key field does not reveal a credential:

![ZCode custom provider pointing to the local proxy](screenshot/screenshot_2.png)

## Windows Launchers and Options

`run_proxy.ps1` activates `.venv` and checks the required credentials. The two dedicated debug launchers also enable OpenCode sync for `%USERPROFILE%\.config\opencode\opencode.json`:

```bat
start_proxy_pool_debug.bat
start_proxy_client_debug.bat
```

The Pool launcher reads `.env`; the Client launcher forwards each client's NVIDIA key. Both use a 600-second upstream timeout and port `18787`. Stop the running proxy with `Ctrl+C` before switching modes. A saved OpenCode credential must match the newly selected mode. For no OpenCode file writes or another config path, call `run_proxy.ps1` directly:

On Windows, `WinError 10013` during startup can mean the listening port is reserved even when no process owns it. Check with `netsh interface ipv4 show excludedportrange protocol=tcp`. The old default, `8787`, may fall inside a Windows-reserved range; the new default is `18787`. To override it for the current PowerShell session, set `$env:NIM_PROXY_PORT='18788'` before starting the proxy and update every IDE's local base URL to match (for example `http://127.0.0.1:18788/v1`). Do not delete Windows port reservations to fix this. If OpenCode sync is enabled, update `provider.nim-local.options.baseURL` in your private OpenCode config before restarting; direct NVIDIA providers keep their public URL.

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -DebugMode -UpstreamTimeoutSeconds 600
.\run_proxy.ps1 -ApiKeyMode Client -ModelRefreshSeconds 3600
.\run_proxy.ps1 -ApiKeyMode Client -DisableModelDiscovery
.\run_proxy.ps1 -ApiKeyMode Pool -OpenCodeConfig "$HOME\.config\opencode\opencode.json"
```

`start_proxy.bat` forwards arguments to the PowerShell launcher. `-DebugMode` logs stripped **field names** and policy decisions without printing keys or full messages. `-ToolCallTextMode pass` relays raw model-generated `<tool_call>` text; the default `diagnostic` mode replaces that markup with a readable diagnostic instead of treating it as an executable tool call.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `-UpstreamTimeoutSeconds` | `300` | Wait for upstream to start responding; examples use `600` |
| `-MaxConcurrentPerKey` | `1` | Active requests per key |
| `-MaxQueuePerKey` | `4` | Waiting requests per key in Env/Client mode |
| `-MaxTotalQueued` | `32` | Total queued requests |
| `-QueueWaitSeconds` | `180` | Maximum queue wait |
| `-RateLimitCooldownSeconds` | `60` | Fallback cooldown if NVIDIA omits `Retry-After` |
| `-Max5xxFailovers` | `1` | Alternate pool keys after upstream `5xx`/transport error |
| `-ModelRefreshSeconds` | `21600` | Public catalog refresh interval; minimum `30` |
| `-DisableModelDiscovery` | off | Disable `/v1/models` discovery |
| `-OpenCodeConfig` | unset | Opt in to add-only `nim-local` sync in a strict JSON file |

Queue defaults are conservative. Higher concurrency can increase NVIDIA `429` responses. The underlying Python CLI uses lower-case flags such as `--api-key-mode pool`, `--upstream-timeout-seconds 600`, `--opencode-config PATH`, and `--no-model-discovery`. Additional environment overrides include `NIM_PROXY_HOST`, `NIM_PROXY_PORT`, and `NIM_PROXY_UPSTREAM_BASE_URL`.

## Queue, Failover, and Errors

| Symptom or condition | Meaning and action |
| --- | --- |
| `400 Unsupported parameter(s): extra_body` | Original unsanitized request; verify the IDE points to the local `/v1` URL and the proxy is running. |
| `400` context/input+output limit | Lower requested output or compact/start a new conversation. Changing only the IDE's displayed context limit does not enlarge NVIDIA's hosted limit. |
| `401` from local Pool provider | Check that the IDE uses `NIM_PROXY_CLIENT_KEY`, not a real NVIDIA key. |
| `429 proxy_queue_full` | Local queue is full; reduce simultaneous requests. The proxy supplies `Retry-After: 5`. |
| NVIDIA `429` | Upstream rate or quota limit. In Pool mode the key cools down and another untried key may be selected. |
| `504 proxy_queue_timeout` | Request waited past the configured queue limit; check active work and retry later. |
| `504 upstream_timeout` | NVIDIA did not start responding before the upstream timeout; try a shorter request or another model. Client-side timeouts can be shorter than the proxy setting. |
| NVIDIA `500`/`5xx` | Upstream error; Pool mode permits bounded failover before streaming starts. Repeated failures across keys may indicate model or platform availability trouble. |
| Plain-text `<tool_call>` shown as a diagnostic | The model returned text rather than structured OpenAI `tool_calls`; choose a model/profile supporting structured tools. |
| `Client disconnected` log | IDE closed the connection before the proxy could send its final response; a proxy crash is not implied. |

Same-key requests use FIFO queues; different healthy keys can work in parallel. In Pool mode, a key is tried at most once per request. NVIDIA `408`/`429` cools a key, `401`/`403` quarantines it until restart, and `400`/`404`/`422` does not trigger key switching. After an SSE stream starts, the proxy cannot safely retry it on another key. When failover is exhausted, the last upstream response is returned so the client sees its real status.

`GET /health` exposes aggregate queue and pool counts, not credential values. Restarting the proxy clears its in-memory queue but does not erase NVIDIA rate limits or any in-flight work already sent upstream.

## Security and Publication

- Real API keys belong only in your local `.env`, process environment, or IDE credential store. Never copy them into README examples, tracked JSON, screenshots, Git commits, issues, or releases.
- `.env`, `.env.*`, `.venv/`, build artifacts, and local caches are excluded by `.gitignore`. `.env.example` contains random **invalid** values solely to show the expected variable names.
- The default bind address is `127.0.0.1`. Remote binding requires an explicit override and is forbidden in Env mode. If you deliberately expose Client/Pool mode on a network, add TLS termination and firewall controls.
- Pool mode validates the local token without forwarding it to NVIDIA. Client mode forwards the supplied NVIDIA token upstream. Logs omit keys and full message content.
- Keep OpenCode configs, `auth.json`, proxy cache backups, and any exported logs private. A local config backup may contain credentials that predate this proxy.

## Development and Releases

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy nvidia_nim_proxy tests
.\.venv\Scripts\python.exe -m pip wheel --no-deps --wheel-dir dist .
```

CI runs Python tests, lint, type checks, wheel packaging, and Windows launcher checks. The project follows Semantic Versioning; version history is listed in [CHANGELOG.md](CHANGELOG.md). GitHub releases are currently unavailable pending maintainer approval.

Roadmap: verify hosted GLM effort overrides before sending them, add exact profiles for newly documented NVIDIA models, and consider a Windows service wrapper. See [LICENSE](LICENSE) for licensing.
