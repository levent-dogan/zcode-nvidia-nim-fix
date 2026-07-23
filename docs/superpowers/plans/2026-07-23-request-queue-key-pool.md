# Request Queue and NVIDIA API Key Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add FIFO request scheduling for real client-supplied NVIDIA keys and a local-credential pool mode with six-key round-robin selection, cooldown, quarantine, and bounded failover.

**Architecture:** Keep HTTP socket ownership in the existing request handler. Add focused credential, scheduling, and key-pool modules; the handler authenticates, acquires a lease, performs an upstream attempt, and releases the lease in `finally`. Client/env modes use per-fingerprint FIFO lanes, while pool mode uses one fair request queue and selects an available NVIDIA key without exposing secrets.

**Tech Stack:** Python 3.10+ standard library, `ThreadingHTTPServer`, `threading.Condition`, PowerShell 5.1/7, pytest, Ruff, mypy, GitHub Actions.

## Global Constraints

- Preserve `Env` and `Client` behavior; add `Pool` as a backward-compatible mode.
- Default per-key concurrency is exactly `1`.
- Pool-mode `429` failover may try each healthy key at most once.
- Pool-mode `5xx` failover may try exactly one alternate key by default.
- Never retry after response headers or stream bytes have been sent to ZCode.
- Never log, return, or commit local credentials, NVIDIA keys, prompts, or message content.
- `.env` remains ignored; `.env.example` contains placeholders only.
- Runtime remains Python-standard-library-only.
- Windows PowerShell 5.1 and PowerShell 7 syntax must remain valid.
- Preserve the untracked user file `start_proxy_debug.bat`; do not stage or edit it.
- Target release version is `0.2.0`.

---

## File Structure

- Create `nvidia_nim_proxy/credentials.py`: modes, bearer parsing, fingerprints, environment key loading, local credential validation.
- Create `nvidia_nim_proxy/scheduler.py`: per-key FIFO scheduler, bounded waits, leases, statistics.
- Create `nvidia_nim_proxy/key_pool.py`: pool key state, round-robin selection, cooldown, quarantine, retry classification.
- Modify `nvidia_nim_proxy/server.py`: configuration, health output, scheduler/pool integration, upstream attempt lifecycle.
- Create `tests/test_credentials.py`: credential and secret-safety tests.
- Create `tests/test_scheduler.py`: deterministic FIFO, concurrency, capacity, timeout, cleanup tests.
- Create `tests/test_key_pool.py`: selection, cooldown, quarantine, and failover policy tests.
- Modify `tests/test_server.py`: handler integration, response routing, health, and no-retry-after-stream tests.
- Modify `run_proxy.ps1`: pool mode, allowlisted `.env` import, validated queue/failover parameters.
- Modify `.env.example`: local credential and six NVIDIA key placeholders.
- Modify `.github/workflows/ci.yml`: add Windows PowerShell parser validation.
- Modify `README.md`, `CHANGELOG.md`, `pyproject.toml`, and `nvidia_nim_proxy/__init__.py`: usage and version `0.2.0`.

---

### Task 1: Credential Modes and Secret-Safe Loading

**Files:**
- Create: `nvidia_nim_proxy/credentials.py`
- Create: `tests/test_credentials.py`
- Modify: `nvidia_nim_proxy/server.py`

**Interfaces:**
- Produces: `API_KEY_MODE_ENV`, `API_KEY_MODE_CLIENT`, `API_KEY_MODE_POOL`.
- Produces: `extract_bearer_token(value: str | None) -> str | None`.
- Produces: `fingerprint_secret(secret: str) -> str`.
- Produces: `load_pool_keys(environ: Mapping[str, str]) -> tuple[str, ...]`.
- Produces: `CredentialBroker.authorize_pool_client(header: str | None) -> None`.
- Produces: `CredentialBroker.resolve_direct_key(header: str | None) -> str`.
- Consumes: process environment mappings only; no file parsing in Python.

- [ ] **Step 1: Write failing credential tests**

```python
def test_load_pool_keys_orders_numeric_suffixes_and_rejects_duplicates() -> None:
    environ = {
        "NVIDIA_API_KEY_2": "key-two",
        "NVIDIA_API_KEY_1": "key-one",
    }
    assert load_pool_keys(environ) == ("key-one", "key-two")

    with pytest.raises(ValueError, match="duplicate NVIDIA API key"):
        load_pool_keys(
            {"NVIDIA_API_KEY_1": "same-secret", "NVIDIA_API_KEY_2": "same-secret"}
        )


def test_pool_auth_uses_local_key_but_never_returns_it_as_upstream_key() -> None:
    broker = CredentialBroker(
        mode=API_KEY_MODE_POOL,
        env_api_key=None,
        local_client_key="local-only-secret",
    )
    broker.authorize_pool_client("Bearer local-only-secret")
    with pytest.raises(ValueError, match="not available in pool mode"):
        broker.resolve_direct_key("Bearer local-only-secret")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m pytest tests/test_credentials.py -q
```

Expected: collection fails because `nvidia_nim_proxy.credentials` does not exist.

- [ ] **Step 3: Implement the credential module**

Implement immutable mode constants and a broker with constant-time local authentication:

```python
@dataclass(frozen=True)
class CredentialBroker:
    mode: str
    env_api_key: str | None
    local_client_key: str | None

    def authorize_pool_client(self, authorization_header: str | None) -> None:
        if self.mode != API_KEY_MODE_POOL:
            raise ValueError("pool authorization is only available in pool mode")
        supplied = extract_bearer_token(authorization_header)
        expected = self.local_client_key
        if supplied is None or expected is None or not hmac.compare_digest(supplied, expected):
            raise PermissionError("invalid local proxy bearer token")

    def resolve_direct_key(self, authorization_header: str | None) -> str:
        if self.mode == API_KEY_MODE_CLIENT:
            token = extract_bearer_token(authorization_header)
            if token is None:
                raise PermissionError("missing client bearer token")
            return token
        if self.mode == API_KEY_MODE_ENV:
            if not self.env_api_key:
                raise ValueError("missing NVIDIA_API_KEY")
            return self.env_api_key
        raise ValueError("direct NVIDIA key is not available in pool mode")
```

`load_pool_keys` must match only `^NVIDIA_API_KEY_([1-9][0-9]*)$`, trim values, sort by numeric
suffix, reject empty pools, and reject duplicate fingerprints. Move the existing bearer and
fingerprint helpers from `server.py` into this module, then re-export them from `server.py` to
preserve existing imports.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_credentials.py tests/test_server.py -q
```

Expected: all credential tests and all existing server tests pass.

- [ ] **Step 5: Commit**

```powershell
git add nvidia_nim_proxy/credentials.py nvidia_nim_proxy/server.py tests/test_credentials.py tests/test_server.py
git commit -m "Add secret-safe credential modes"
```

---

### Task 2: Per-Key FIFO Scheduler

**Files:**
- Create: `nvidia_nim_proxy/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `QueueFullError(scope: str)`.
- Produces: `QueueWaitTimeoutError`.
- Produces: `QueueLease.wait_ms`, `QueueLease.queue_position`, `QueueLease.release()`.
- Produces: `RequestScheduler.acquire(fingerprint: str, model: str) -> QueueLease`.
- Produces: `RequestScheduler.snapshot() -> SchedulerSnapshot`.

- [ ] **Step 1: Write deterministic scheduler tests**

Use threads and events rather than sleeps for admission assertions:

```python
def test_same_key_is_fifo_while_different_keys_can_run() -> None:
    scheduler = RequestScheduler(
        max_concurrent_per_key=1,
        max_queue_per_key=4,
        max_total_queued=32,
        queue_wait_seconds=2,
    )
    first = scheduler.acquire("key-a", "model-a")
    admitted: list[str] = []

    thread = Thread(
        target=lambda: _acquire_record_release(scheduler, "key-a", "second", admitted)
    )
    thread.start()
    _wait_until(lambda: scheduler.snapshot().queued == 1)

    other = scheduler.acquire("key-b", "model-b")
    admitted.append("other")
    other.release()
    assert admitted == ["other"]

    first.release()
    thread.join(timeout=2)
    assert admitted == ["other", "second"]
```

Add separate tests for per-key queue full, global queue full, timeout removal, idempotent release,
exception-safe context manager use, and idle lane cleanup.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m pytest tests/test_scheduler.py -q
```

Expected: collection fails because `nvidia_nim_proxy.scheduler` does not exist.

- [ ] **Step 3: Implement scheduler admission and release**

Use one `threading.Condition`, a lane dictionary, and opaque ticket objects:

```python
class RequestScheduler:
    def acquire(self, fingerprint: str, model: str) -> QueueLease:
        deadline = time.monotonic() + self.queue_wait_seconds
        with self._condition:
            lane = self._lanes.setdefault(fingerprint, _Lane())
            immediate = lane.active < self.max_concurrent_per_key and not lane.waiters
            if not immediate:
                self._check_capacity(lane)
            ticket = _Ticket(model=model, enqueued_at=time.monotonic())
            lane.waiters.append(ticket)
            self._queued += 1
            position = len(lane.waiters)
            while lane.waiters[0] is not ticket or lane.active >= self.max_concurrent_per_key:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_waiter(fingerprint, lane, ticket)
                    raise QueueWaitTimeoutError
                self._condition.wait(remaining)
            lane.waiters.popleft()
            self._queued -= 1
            lane.active += 1
            return QueueLease(
                scheduler=self,
                fingerprint=fingerprint,
                queue_position=position,
                wait_ms=int((time.monotonic() - ticket.enqueued_at) * 1000),
            )
```

Release decrements active count, removes empty lanes, and calls `notify_all()`. Make release
idempotent under the same condition lock.

- [ ] **Step 4: Run focused tests and type checks**

Run:

```powershell
python -m pytest tests/test_scheduler.py -q
python -m mypy nvidia_nim_proxy/scheduler.py tests/test_scheduler.py
```

Expected: scheduler tests pass and mypy reports no issues.

- [ ] **Step 5: Commit**

```powershell
git add nvidia_nim_proxy/scheduler.py tests/test_scheduler.py
git commit -m "Add per-key FIFO request scheduler"
```

---

### Task 3: Round-Robin NVIDIA Key Pool and Failover Policy

**Files:**
- Create: `nvidia_nim_proxy/key_pool.py`
- Create: `tests/test_key_pool.py`
- Consume: `nvidia_nim_proxy/credentials.py`

**Interfaces:**
- Produces: `PoolUnavailableError`, `PoolQueueFullError`, `PoolWaitTimeoutError`.
- Produces: `PoolLease.secret`, `PoolLease.fingerprint`, `PoolLease.release(...)`.
- Produces: `NvidiaKeyPool.acquire(attempted: frozenset[str]) -> PoolLease`.
- Produces: `NvidiaKeyPool.should_failover(status: int, five_xx_failovers: int) -> bool`.
- Produces: `NvidiaKeyPool.snapshot() -> KeyPoolSnapshot`.

- [ ] **Step 1: Write key-pool behavior tests**

```python
def test_round_robin_cooldown_and_quarantine() -> None:
    clock = FakeClock()
    pool = NvidiaKeyPool(
        ("key-one", "key-two", "key-three"),
        max_concurrent_per_key=1,
        max_total_queued=8,
        queue_wait_seconds=2,
        default_cooldown_seconds=60,
        max_5xx_failovers=1,
        clock=clock,
    )

    first = pool.acquire(frozenset())
    first_fp = first.fingerprint
    first.release(status=429, retry_after="30")

    second = pool.acquire(frozenset({first_fp}))
    assert second.fingerprint != first_fp
    second.release(status=401, retry_after=None)

    snapshot = pool.snapshot()
    assert snapshot.cooling_down == 1
    assert snapshot.quarantined == 1
    assert snapshot.available == 1
```

Add tests proving numeric `Retry-After` and HTTP-date parsing, all healthy keys are attempted only
once for `429`, only one alternate is permitted for `5xx`, `400` is final, all-busy requests queue
FIFO, and snapshots contain counts rather than fingerprints.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m pytest tests/test_key_pool.py -q
```

Expected: collection fails because `nvidia_nim_proxy.key_pool` does not exist.

- [ ] **Step 3: Implement key state and lease lifecycle**

Use private mutable key records and public immutable snapshots:

```python
@dataclass
class _KeyState:
    secret: str
    fingerprint: str
    active: int = 0
    cooldown_until: float = 0.0
    quarantined: bool = False


def classify_failover(status: int, five_xx_failovers: int, limit: int) -> bool:
    if status in {401, 403, 408, 429}:
        return True
    if status in {500, 502, 503, 504}:
        return five_xx_failovers < limit
    return False
```

Selection starts after the previous round-robin index, skips attempted/busy/cooling/quarantined
records, and increments `active` before returning a lease. A `429/408` release sets cooldown from
`Retry-After` or the configured fallback. A `401/403` release quarantines only that key. Every
release notifies waiting threads.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m pytest tests/test_key_pool.py -q
python -m mypy nvidia_nim_proxy/key_pool.py tests/test_key_pool.py
```

Expected: all key-pool tests pass and mypy reports no issues.

- [ ] **Step 5: Commit**

```powershell
git add nvidia_nim_proxy/key_pool.py tests/test_key_pool.py
git commit -m "Add NVIDIA key pool failover"
```

---

### Task 4: Integrate Scheduling and Bounded Failover into HTTP Handling

**Files:**
- Modify: `nvidia_nim_proxy/server.py`
- Modify: `tests/test_server.py`
- Consume: `nvidia_nim_proxy/credentials.py`
- Consume: `nvidia_nim_proxy/scheduler.py`
- Consume: `nvidia_nim_proxy/key_pool.py`

**Interfaces:**
- `ProxyConfig` gains scheduler and pool settings plus initialized broker/scheduler/pool members.
- `build_upstream_chat_request(..., api_key_override: str | None = None)` supports pool-selected keys.
- `NIMProxyHandler._forward_direct(...)` handles env/client modes.
- `NIMProxyHandler._forward_from_pool(...)` handles local auth and bounded failover.
- `NIMProxyHandler._open_upstream_attempt(...)` opens a connection without sending client headers.

- [ ] **Step 1: Write handler integration tests**

Add fake HTTP response/connection objects that record authorization headers and response relay:

```python
def test_pool_429_switches_key_without_forwarding_local_secret() -> None:
    handler, attempts = build_pool_handler(
        statuses=[429, 200],
        nvidia_keys=("nvidia-one", "nvidia-two"),
        local_key="local-proxy-secret",
    )

    handler._forward_to_nim(
        {"model": "z-ai/glm-5.2", "messages": [], "stream": False},
        client_authorization="Bearer local-proxy-secret",
    )

    assert [item.authorization for item in attempts] == [
        "Bearer nvidia-one",
        "Bearer nvidia-two",
    ]
    assert all("local-proxy-secret" not in item.authorization for item in attempts)
```

Add tests for: invalid local key returns `401`; `500,500` creates exactly two attempts; `400`
creates one attempt; client mode same-key serialization uses `RequestScheduler`; final upstream
status/body passes through; a `200` streaming response never retries after relay begins; queue
full and timeout return machine-readable local errors; health returns counts only.

- [ ] **Step 2: Run integration tests and verify failure**

Run:

```powershell
python -m pytest tests/test_server.py -q
```

Expected: new pool and scheduler assertions fail while existing tests remain collectible.

- [ ] **Step 3: Refactor upstream construction**

Allow an explicit pool-selected key while preserving direct-mode resolution:

```python
api_key = (
    api_key_override
    if api_key_override is not None
    else config.credential_broker.resolve_direct_key(client_authorization)
)
```

Keep the raw key only in the outgoing Authorization header. Continue returning only its
fingerprint in debug-safe request metadata.

- [ ] **Step 4: Add Python CLI and startup validation**

Extend `--api-key-mode` with `pool`. Add typed arguments for every queue and failover setting,
using `positive_int` except `--max-5xx-failovers`, which accepts zero:

```python
parser.add_argument(
    "--api-key-mode",
    choices=(API_KEY_MODE_ENV, API_KEY_MODE_CLIENT, API_KEY_MODE_POOL),
    default=os.getenv("NIM_PROXY_API_KEY_MODE", API_KEY_MODE_ENV),
)
parser.add_argument(
    "--max-concurrent-per-key",
    type=positive_int,
    default=os.getenv("NIM_PROXY_MAX_CONCURRENT_PER_KEY", "1"),
)
parser.add_argument(
    "--max-5xx-failovers",
    type=non_negative_int,
    default=os.getenv("NIM_PROXY_MAX_5XX_FAILOVERS", "1"),
)
```

Add corresponding arguments for per-key queue `4`, total queue `32`, queue wait `180`, and
cooldown `60`. In `main`, fail closed before binding:

- env mode requires `NVIDIA_API_KEY`
- pool mode requires `NIM_PROXY_CLIENT_KEY`
- pool mode calls `load_pool_keys(os.environ)` and rejects empty or duplicate pools
- client mode does not preload NVIDIA keys

Keep non-loopback binding disabled unless `--allow-remote` is explicit. Continue prohibiting
remote env mode. Permit client and pool modes only after explicit opt-in, and log a warning that
the operator must provide network encryption and firewall controls.

- [ ] **Step 5: Implement direct-mode queue lifecycle**

Resolve the direct key, acquire its fingerprint lane, log admission, call the existing relay
path, and release in `finally`. Convert `QueueFullError` to local `429` and
`QueueWaitTimeoutError` to local `504` with structured error codes.

- [ ] **Step 6: Implement pool-mode attempt loop**

Use one attempted-fingerprint set and one `five_xx_failovers` counter:

```python
attempted: set[str] = set()
five_xx_failovers = 0
while True:
    lease = self.config.key_pool.acquire(frozenset(attempted))
    attempted.add(lease.fingerprint)
    try:
        connection, response, request, elapsed_ms = self._open_upstream_attempt(
            body,
            api_key=lease.secret,
        )
        status = response.status
        retry_after = response.getheader("Retry-After")
        if self.config.key_pool.should_failover(status, five_xx_failovers):
            if 500 <= status <= 599:
                five_xx_failovers += 1
            response.read()
            lease.release(status=status, retry_after=retry_after)
            connection.close()
            continue
        self._relay_upstream_response(response, stream=request.stream)
        lease.release(status=status, retry_after=retry_after)
        connection.close()
        return
    finally:
        lease.release_if_active()
```

Connection errors before response headers consume one `5xx` failover allowance. Never enter the
attempt loop from `_relay_remaining_stream_after_headers`.

- [ ] **Step 7: Expand health and debug-safe logs**

Return:

```json
{
  "status": "ok",
  "version": "0.2.0",
  "mode": "pool",
  "queue": {"active": 2, "queued": 1},
  "key_pool": {
    "total": 6,
    "available": 4,
    "cooling_down": 1,
    "quarantined": 0,
    "active": 1
  }
}
```

Omit `key_pool` outside pool mode. Assert that serialized health output contains neither raw keys
nor fingerprints.

- [ ] **Step 8: Run full Python validation**

Run:

```powershell
python -m pytest
python -m ruff check .
python -m mypy nvidia_nim_proxy tests
```

Expected: all tests pass, Ruff reports no violations, and mypy reports no issues.

- [ ] **Step 9: Commit**

```powershell
git add nvidia_nim_proxy/server.py tests/test_server.py
git commit -m "Integrate queued NVIDIA failover"
```

---

### Task 5: Windows Launcher, Safe `.env`, and CI Validation

**Files:**
- Modify: `run_proxy.ps1`
- Modify: `.env.example`
- Modify: `.github/workflows/ci.yml`
- Do not modify: `start_proxy_debug.bat`

**Interfaces:**
- Adds launcher `-ApiKeyMode Pool`.
- Adds `-EnvFile`, `-MaxConcurrentPerKey`, `-MaxQueuePerKey`,
  `-MaxTotalQueued`, `-QueueWaitSeconds`, `-RateLimitCooldownSeconds`,
  `-Max5xxFailovers`.
- Loads only `NIM_PROXY_CLIENT_KEY` and `NVIDIA_API_KEY_<number>` from `.env`.

- [ ] **Step 1: Add the placeholder environment template**

Replace `.env.example` with:

```dotenv
# Never put real keys in a committed file.
NIM_PROXY_CLIENT_KEY=REPLACE_WITH_A_LOCAL_PROXY_SECRET
NVIDIA_API_KEY_1=REPLACE_WITH_NVIDIA_KEY_1
NVIDIA_API_KEY_2=REPLACE_WITH_NVIDIA_KEY_2
NVIDIA_API_KEY_3=REPLACE_WITH_NVIDIA_KEY_3
NVIDIA_API_KEY_4=REPLACE_WITH_NVIDIA_KEY_4
NVIDIA_API_KEY_5=REPLACE_WITH_NVIDIA_KEY_5
NVIDIA_API_KEY_6=REPLACE_WITH_NVIDIA_KEY_6
```

- [ ] **Step 2: Implement allowlisted `.env` loading**

Use `ConvertFrom-StringData` and never execute the file:

```powershell
$ParsedValues = ConvertFrom-StringData (Get-Content -LiteralPath $ResolvedEnvFile -Raw)
foreach ($Entry in $ParsedValues.GetEnumerator()) {
    $Allowed = $Entry.Key -eq "NIM_PROXY_CLIENT_KEY" -or
        $Entry.Key -match '^NVIDIA_API_KEY_[1-9][0-9]*$'
    if (-not $Allowed) {
        Stop-WithMessage "Unsupported variable in pool env file: $($Entry.Key)"
    }
    [Environment]::SetEnvironmentVariable($Entry.Key, [string]$Entry.Value, "Process")
}
```

In pool mode, require the file, local credential, and at least one numbered NVIDIA key. Print only
the number of imported NVIDIA keys.

- [ ] **Step 3: Add validated launcher parameters**

Use `ValidateRange` attributes and pass exact corresponding server arguments. Keep the normal
recommended command:

```powershell
.\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

- [ ] **Step 4: Add Windows CI parser validation**

Add a `launcher` job using `windows-latest`:

```yaml
launcher:
  runs-on: windows-latest
  steps:
    - uses: actions/checkout@v6
    - name: Validate PowerShell launcher syntax
      shell: pwsh
      run: |
        $tokens = $null
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
          (Resolve-Path .\run_proxy.ps1),
          [ref]$tokens,
          [ref]$errors
        )
        if ($errors.Count -gt 0) {
          $errors | ForEach-Object { Write-Error $_.Message }
          exit 1
        }
```

- [ ] **Step 5: Validate locally without exposing keys**

Run:

```powershell
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path .\run_proxy.ps1),
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count -gt 0) { throw ($errors | Out-String) }
```

Expected: no parser errors. Do not start the launcher with real credentials during automated
tests.

- [ ] **Step 6: Commit**

```powershell
git add .env.example .github/workflows/ci.yml run_proxy.ps1
git commit -m "Add Windows key pool launcher"
```

---

### Task 6: Documentation, Versioning, and Release Validation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `SECURITY.md`
- Modify: `pyproject.toml`
- Modify: `nvidia_nim_proxy/__init__.py`

**Interfaces:**
- Documents exact Env, Client, and Pool commands.
- Publishes package version `0.2.0`.

- [ ] **Step 1: Update version and changelog**

Set both version declarations to:

```toml
version = "0.2.0"
```

```python
__version__ = "0.2.0"
```

Add a `0.2.0` changelog entry covering FIFO queues, pool authentication, round-robin key
selection, bounded failover, cooldown/quarantine, health counters, Windows launcher changes, and
security constraints.

- [ ] **Step 2: Document all three launch modes**

Include these exact examples:

```powershell
# One environment-backed NVIDIA key
.\run_proxy.ps1 -ApiKeyMode Env

# Real NVIDIA key supplied by each ZCode provider, queued per key
.\run_proxy.ps1 -ApiKeyMode Client -UpstreamTimeoutSeconds 600

# One local ZCode credential backed by the private NVIDIA key pool
.\run_proxy.ps1 -ApiKeyMode Pool -UpstreamTimeoutSeconds 600
```

Explain `.env` creation, ZCode Base URL, local credential placement, queue defaults, status-based
failover, streaming limitations, health output, and safe troubleshooting.

- [ ] **Step 3: Update security guidance**

Document local credential rotation, `.env` file permissions, loopback binding, remote-mode risks,
secret-safe logs, and the rule that `.env` must never be committed.

- [ ] **Step 4: Run complete release checks**

Run:

```powershell
python -m pytest
python -m ruff check .
python -m mypy nvidia_nim_proxy tests
py -3.11 -m build --wheel
git diff --check
rg -n "nvapi-|Bearer [A-Za-z0-9_-]{20,}|NVIDIA_API_KEY_[0-9]+=.+" .
```

Expected:

- all tests pass
- Ruff and mypy pass
- wheel `zcode_nvidia_nim_fix-0.2.0-py3-none-any.whl` builds
- no whitespace errors
- secret scan finds only documented placeholders or test-only dummy values

- [ ] **Step 5: Commit**

```powershell
git add CHANGELOG.md README.md SECURITY.md pyproject.toml nvidia_nim_proxy/__init__.py
git commit -m "Document queued key pool mode"
```

- [ ] **Step 6: Verify final repository state**

Run:

```powershell
git status --short
git log --oneline -8
```

Expected: only the pre-existing untracked `start_proxy_debug.bat` remains; all implementation
commits are present and no real `.env` file is staged.
