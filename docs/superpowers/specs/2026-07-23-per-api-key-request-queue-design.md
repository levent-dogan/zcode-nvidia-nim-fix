# Per-API-Key Request Queue Design

Date: 2026-07-23
Status: Approved for implementation

## Problem

The proxy currently uses a threaded HTTP server and forwards every incoming ZCode request to
NVIDIA NIM immediately. Several projects can therefore send concurrent requests through the
same NVIDIA API key. That can increase upstream `429` responses, latency, and transient `5xx`
responses.

The proxy must serialize requests that share an NVIDIA API key while preserving parallelism
between different keys. Each response must continue to return to the ZCode request that created
it, including streaming responses.

## Selected Approach

Add an in-memory FIFO scheduler with one independent lane per NVIDIA API key:

- One request per API key may call NVIDIA NIM at a time by default.
- Requests using different API keys may call NVIDIA NIM concurrently.
- Waiting requests are admitted in FIFO order within their key lane.
- The existing HTTP handler retains ownership of its client connection while it waits and while
  it relays the NVIDIA response. The scheduler does not move sockets between threads.
- The API key fingerprint is the lane identifier used for logs and diagnostics. The real API key
  remains only in memory and is never logged.
- The request body retains its original `model`, so the existing upstream builder routes it to
  the correct NVIDIA model.

In `client` API key mode, different ZCode provider keys create different lanes. In `env` mode,
all requests intentionally share the single `NVIDIA_API_KEY` lane.

## Alternatives Considered

### One global queue

This is simple but would make one slow model or key block every other project. It does not fit
the six-key usage pattern.

### One lane per API key and model

This allows the same key to run several models concurrently. It provides more throughput, but it
does not protect against limits enforced at API-key or account level. It can be added later as an
opt-in policy if NVIDIA publishes reliable per-model concurrency limits.

### Per-API-key FIFO lanes

This is the selected design. It is conservative, predictable, and directly addresses concurrent
use of the same key without reducing concurrency across independent keys.

## Scheduler Components

Introduce a focused `RequestScheduler` component and a context-managed `QueueLease`:

- `RequestScheduler.acquire(key_fingerprint, model)` enqueues a ticket and waits for admission.
- `QueueLease.release()` runs in a `finally` path after the upstream request ends or fails.
- Each lane tracks its active count, FIFO tickets, and last-use state under a condition lock.
- Empty inactive lanes are removed so fingerprints do not accumulate indefinitely.
- Scheduler state stores fingerprints and model names only. It never stores API keys, prompts,
  message content, or response content.

The handler prepares the upstream request first so authentication and the key fingerprint are
available. It then acquires a lease before opening the NVIDIA connection. Existing non-streaming
and streaming relay logic remains unchanged after admission.

## Configuration

Add CLI arguments, environment variables, and PowerShell launcher parameters:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `NIM_PROXY_MAX_CONCURRENT_PER_KEY` | `1` | Active NVIDIA requests allowed for one key |
| `NIM_PROXY_MAX_QUEUE_PER_KEY` | `4` | Waiting requests allowed for one key |
| `NIM_PROXY_MAX_TOTAL_QUEUED` | `32` | Process-wide waiting-request safety limit |
| `NIM_PROXY_QUEUE_WAIT_SECONDS` | `180` | Maximum time a request may wait for admission |

The PowerShell launcher will expose corresponding validated parameters. Queueing will be enabled
by default. Setting per-key concurrency above one remains available for users who prefer
throughput over conservative rate-limit protection.

## Error Behavior

- A full per-key or global queue returns a local `429` response with a short `Retry-After` header.
- A queue wait timeout returns a local `504` response with a machine-readable
  `proxy_queue_timeout` error.
- Authentication and invalid upstream URL errors are resolved before queue admission.
- A lease is always released after upstream success, failure, timeout, or client disconnect.
- NVIDIA status codes continue to pass through unchanged.
- This release does not automatically retry NVIDIA requests. Queueing reduces concurrency but
  cannot guarantee that NVIDIA will not return a genuine `500`, and hidden retries would increase
  latency and complicate streaming semantics.

Debug-safe logs will include:

- event type (`queued`, `admitted`, `released`, `queue_full`, or `queue_timeout`)
- API key fingerprint
- model
- queue position
- wait duration

No log entry may include an API key or message content.

## Response Routing

No explicit project identifier is required to return a response correctly. Every handler keeps
the original ZCode TCP connection and writes the result back over that connection after it is
admitted.

If separate projects use different NVIDIA keys, fingerprints also provide project-level
diagnostic separation without exposing project names. If projects share one key, the proxy can
preserve FIFO order and correct response routing but cannot identify project names because ZCode
does not send a project identifier.

## Streaming and Cancellation

Queue admission occurs before NVIDIA response headers are sent. Once admitted, the existing SSE
relay path remains responsible for streaming bytes to the originating ZCode connection.

A client can disconnect while waiting. The first implementation will rely on existing disconnect
handling when response headers or stream bytes are written. Proactive cancellation of a queued
ticket is outside this release because the standard HTTP server does not provide a reliable
portable disconnect notification while the handler is waiting. A request admitted immediately
after its client disconnects can therefore still consume one upstream call.

## Tests

Add deterministic unit tests covering:

- same-key requests never exceed the configured active limit
- same-key tickets are admitted in FIFO order
- different API keys can be active concurrently
- per-key queue capacity enforcement
- global queue capacity enforcement
- queue wait timeout
- lease release after exceptions
- idle lane cleanup
- API keys and message content are absent from scheduler state and logs
- existing streaming, sanitizer, authentication, timeout, and tool-call tests remain green
- PowerShell launcher syntax and parameter validation

## Documentation and Versioning

Document normal and advanced queue settings, multi-project behavior, limitations, and debug log
examples in `README.md`. Add the change to `CHANGELOG.md`.

Because this is a backward-compatible runtime feature with new configuration, release it as
version `0.2.0`.
