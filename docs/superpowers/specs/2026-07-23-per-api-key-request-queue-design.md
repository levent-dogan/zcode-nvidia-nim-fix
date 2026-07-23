# Request Queue and NVIDIA API Key Pool Design

Date: 2026-07-23
Status: Approved design; pending written specification review

## Problem

The proxy currently uses a threaded HTTP server and forwards every incoming ZCode request to
NVIDIA NIM immediately. Several projects can therefore send concurrent requests through the
same NVIDIA API key. That can increase upstream `429` responses, latency, and transient `5xx`
responses.

The proxy needs two complementary operating models:

1. ZCode providers can continue sending their own real NVIDIA API keys. Requests that share a key
   must enter the same FIFO lane.
2. ZCode providers can use one local proxy credential while the proxy selects from a private pool
   of NVIDIA API keys. The pool must queue requests, rotate keys, cool down rate-limited keys, and
   perform bounded failover.

Every response must return to the ZCode HTTP request that created it, including streaming
responses. No NVIDIA key or message content may be logged or committed.

## Supported API Key Modes

### Client mode

ZCode sends a real NVIDIA key in its `Authorization: Bearer` header. The proxy forwards that key
to NVIDIA after acquiring the FIFO lane associated with its fingerprint.

- Same NVIDIA key: serialized by default.
- Different NVIDIA keys: may execute concurrently.
- Failover: unavailable because the proxy knows only the key supplied with that request.

This preserves the current multi-provider workflow.

### Pool mode

Every ZCode provider sends the same local proxy credential. The proxy validates it using a
constant-time comparison and never forwards it to NVIDIA. It then selects an NVIDIA key from the
private pool.

- One active request per NVIDIA key by default.
- Available keys are selected using round-robin order.
- Busy or cooling-down keys are skipped.
- Waiting requests enter a bounded process-wide FIFO queue.
- The selected NVIDIA key is used only for that upstream attempt.
- A retry can select a different healthy key according to the failover policy.

### Env mode

The existing single `NVIDIA_API_KEY` behavior remains available for backward compatibility. All
requests intentionally share one key lane. Pool functionality is not enabled in this mode.

## Local Environment File

The Windows launcher will load a repository-root `.env` file in pool mode. It will import only
the documented variable names into the child process and will not print their values:

```dotenv
NIM_PROXY_CLIENT_KEY=REPLACE_WITH_A_LOCAL_PROXY_SECRET
NVIDIA_API_KEY_1=REPLACE_WITH_NVIDIA_KEY_1
NVIDIA_API_KEY_2=REPLACE_WITH_NVIDIA_KEY_2
NVIDIA_API_KEY_3=REPLACE_WITH_NVIDIA_KEY_3
NVIDIA_API_KEY_4=REPLACE_WITH_NVIDIA_KEY_4
NVIDIA_API_KEY_5=REPLACE_WITH_NVIDIA_KEY_5
NVIDIA_API_KEY_6=REPLACE_WITH_NVIDIA_KEY_6
```

`.env` remains ignored by Git. The repository will contain only `.env.example` with placeholders.
The launcher will use PowerShell's structured string-data parser and an allowlist instead of
executing or dot-sourcing `.env` content.

Direct `python -m nvidia_nim_proxy.server` usage can provide the same variables through the
process environment without using the launcher.

## Scheduler and Credential Components

The implementation will separate responsibilities:

- `RequestScheduler` owns FIFO admission, concurrency limits, capacity limits, wait timeouts, and
  lease release.
- `CredentialBroker` handles client, env, and pool authentication without logging secrets.
- `NvidiaKeyPool` owns round-robin key selection, key health, cooldowns, quarantine, and bounded
  failover decisions.
- `QueueLease` guarantees release from a `finally` path after success, error, timeout, or client
  disconnect.

The handler retains ownership of its client socket while waiting and relaying. Worker threads do
not exchange sockets. Scheduler state stores fingerprints, model names, counters, and timestamps
only. The raw keys remain in the credential broker's in-memory configuration.

Idle client/env lanes are removed when they have no active or waiting requests. Pool key state
remains for the process lifetime so cooldown and quarantine decisions are preserved.

## Request Flow

### Client and env modes

1. Read and sanitize the OpenAI-compatible request.
2. Resolve the NVIDIA key and fingerprint.
3. Enter the fingerprint's FIFO lane.
4. Build and send the NVIDIA request.
5. Relay the response to the originating ZCode connection.
6. Release the lane in `finally`.

### Pool mode

1. Read and sanitize the OpenAI-compatible request.
2. Validate the local proxy bearer credential.
3. Enter the shared FIFO pool queue.
4. Select an available NVIDIA key using round-robin order.
5. Send the request and classify the upstream status.
6. When policy allows, release or cool down that key and retry with another healthy key.
7. Relay the final response to the originating ZCode connection.
8. Release all scheduler and key leases in `finally`.

The body retains its original `model`, so every attempt targets the model selected by the
originating ZCode project.

## Queue Configuration

Add CLI arguments, environment variables, and validated PowerShell launcher parameters:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `NIM_PROXY_MAX_CONCURRENT_PER_KEY` | `1` | Active requests allowed for one NVIDIA key |
| `NIM_PROXY_MAX_QUEUE_PER_KEY` | `4` | Waiting requests allowed for one client/env key |
| `NIM_PROXY_MAX_TOTAL_QUEUED` | `32` | Process-wide waiting-request limit |
| `NIM_PROXY_QUEUE_WAIT_SECONDS` | `180` | Maximum time a request may wait for admission |
| `NIM_PROXY_RATE_LIMIT_COOLDOWN_SECONDS` | `60` | Fallback cooldown when NVIDIA omits `Retry-After` |
| `NIM_PROXY_MAX_5XX_FAILOVERS` | `1` | Alternate keys tried after the first `5xx` response |

Queueing is enabled by default. Increasing per-key concurrency is an explicit throughput
trade-off.

## Failover Policy

The selected policy is bounded and status-aware:

| NVIDIA result | Pool action |
| --- | --- |
| `2xx` | Return response and keep key healthy |
| `400`, `404`, `422` | Return response; do not rotate because the request/model is invalid for every key |
| `401`, `403` | Quarantine that NVIDIA key until restart and try another healthy key |
| `408`, `429` | Cool down the key using `Retry-After` or 60 seconds, then try another healthy key |
| `500`, `502`, `503`, `504` | Try at most one alternate healthy key |
| Connection error before response headers | Treat like a `5xx` and try at most one alternate key |

For `429`, the request can move through all currently healthy keys at most once. A per-request set
of attempted fingerprints prevents loops.

For `5xx`, only one alternate key is attempted. If it also fails, the latest NVIDIA response is
returned. This avoids multiplying traffic when the selected model has a provider-wide outage.

No retry occurs after any response headers or streaming bytes have been sent to ZCode. A stream
that stalls after output begins is closed using the existing behavior.

## Queue Error Behavior

- A full per-key or global queue returns a local `429` response with `Retry-After`.
- A queue wait timeout returns local `504` with `error.code = "proxy_queue_timeout"`.
- Missing or incorrect local credentials in pool mode return `401`.
- No healthy pool key before the queue deadline returns `503` with
  `error.code = "nvidia_key_pool_unavailable"`.
- NVIDIA status codes pass through unchanged when failover is not allowed or has been exhausted.

Local proxy errors include a machine-readable error code and a debug-safe log source so they can
be distinguished from NVIDIA responses.

## Response Routing and Project Identity

The originating handler keeps the ZCode TCP connection, so each result returns to the correct
project even when requests are queued or retried.

Pool mode deliberately uses one local credential and does not receive a project identifier.
Therefore the proxy can route responses correctly but cannot name the originating project in
logs. Logs can identify the selected model and NVIDIA key fingerprint without exposing either
credential.

## Logging and Health

Debug-safe queue logs include:

- event (`queued`, `admitted`, `released`, `cooldown`, `quarantined`, `failover`,
  `queue_full`, or `queue_timeout`)
- NVIDIA key fingerprint where a key has been selected
- model
- queue position
- attempt number
- wait and upstream elapsed milliseconds
- upstream status and parsed `Retry-After`

The health endpoint will report mode, total key count, available key count, cooling-down count,
quarantined count, active requests, and queued requests. It will never expose raw keys,
fingerprints, prompts, or response content.

## Security

- `.env`, `.env.*`, and real key files remain ignored.
- `.env.example` contains placeholders only.
- Local and NVIDIA credentials are never logged or returned by health endpoints.
- Local credential validation uses `hmac.compare_digest`.
- Duplicate NVIDIA keys are rejected by fingerprint during startup.
- Pool mode fails closed if the local credential is missing or fewer than one NVIDIA key loads.
- Non-loopback binding remains disabled by default.
- Remote pool mode requires explicit `--allow-remote`; TLS termination and host firewall controls
  remain the operator's responsibility.
- Startup logs show only the number of loaded keys.

## Streaming and Cancellation

Queue and failover decisions occur before NVIDIA response headers are sent to ZCode. Once a
successful stream begins, the existing SSE relay owns the connection and no failover is possible.

A client can disconnect while waiting. Proactive cancellation of a queued ticket is outside this
release because the standard HTTP server does not provide reliable portable disconnect
notification while the handler is waiting. A request admitted immediately after its client
disconnects can therefore consume one upstream call.

## Tests

Add deterministic tests for:

- same-key client requests are FIFO and respect concurrency
- different client keys execute concurrently
- pool mode rejects an invalid local credential
- pool mode never forwards the local credential
- round-robin selection across six placeholder keys
- busy and cooling-down keys are skipped
- `Retry-After` parsing and fallback cooldown
- `429` tries each healthy key at most once
- `5xx` uses at most one alternate key
- `400` does not rotate
- `401/403` quarantines only the affected key
- no retry after response relay starts
- per-key and global queue limits
- queue wait and unavailable-pool errors
- lease release after exceptions and disconnects
- duplicate key rejection
- health output contains counts but no fingerprints or secrets
- `.env` launcher loading uses an allowlist and does not print values
- existing sanitizer, streaming, timeout, authentication, and tool-call behavior remains green
- PowerShell 5.1 and PowerShell 7 parser compatibility

## Documentation and Versioning

Update `README.md` with separate `Env`, `Client`, and `Pool` setup instructions, a safe
`.env.example`, queue/failover behavior, launch commands, troubleshooting, and security
limitations. Update `CHANGELOG.md`.

Because this is a backward-compatible feature set with new modes and configuration, release it as
version `0.2.0`.
