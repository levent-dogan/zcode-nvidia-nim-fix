from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from threading import Event, Thread
from typing import Callable

import pytest

from nvidia_nim_proxy.credentials import fingerprint_secret
from nvidia_nim_proxy.key_pool import (
    NvidiaKeyPool,
    PoolQueueFullError,
    PoolUnavailableError,
    PoolWaitTimeoutError,
    parse_retry_after_seconds,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def _pool(
    keys: tuple[str, ...] = ("key-one", "key-two", "key-three"),
    *,
    clock: Callable[[], float] = time.monotonic,
    queue_wait_seconds: float = 2.0,
    max_total_queued: int = 8,
) -> NvidiaKeyPool:
    return NvidiaKeyPool(
        keys,
        max_concurrent_per_key=1,
        max_total_queued=max_total_queued,
        queue_wait_seconds=queue_wait_seconds,
        default_cooldown_seconds=60,
        max_5xx_failovers=1,
        clock=clock,
    )


def test_round_robin_selection_cycles_through_keys() -> None:
    pool = _pool()
    fingerprints: list[str] = []

    for _ in range(4):
        lease = pool.acquire(frozenset())
        fingerprints.append(lease.fingerprint)
        lease.release(status=200, retry_after=None)

    assert fingerprints == [
        fingerprint_secret("key-one"),
        fingerprint_secret("key-two"),
        fingerprint_secret("key-three"),
        fingerprint_secret("key-one"),
    ]


def test_busy_keys_are_skipped_and_waiting_request_is_admitted() -> None:
    pool = _pool(("key-one", "key-two"))
    first = pool.acquire(frozenset())
    second = pool.acquire(frozenset())
    admitted = Event()
    release_waiter = Event()

    def wait_for_key() -> None:
        with pool.acquire(frozenset()) as lease:
            admitted.set()
            release_waiter.wait(timeout=2)
            lease.set_result(status=200, retry_after=None)

    waiter = Thread(target=wait_for_key)
    waiter.start()
    _wait_until(lambda: pool.snapshot().queued == 1)

    first.release(status=200, retry_after=None)
    assert admitted.wait(timeout=2)
    release_waiter.set()
    waiter.join(timeout=2)
    second.release(status=200, retry_after=None)

    assert not waiter.is_alive()
    assert pool.snapshot().active == 0


def test_cooldown_quarantine_and_expiry_are_key_specific() -> None:
    clock = FakeClock()
    pool = _pool(clock=clock)

    first = pool.acquire(frozenset())
    first_fp = first.fingerprint
    first.release(status=429, retry_after="30")

    second = pool.acquire(frozenset({first_fp}))
    second_fp = second.fingerprint
    second.release(status=401, retry_after=None)

    third = pool.acquire(frozenset({first_fp, second_fp}))
    third.release(status=200, retry_after=None)

    snapshot = pool.snapshot()
    assert snapshot.total == 3
    assert snapshot.available == 1
    assert snapshot.cooling_down == 1
    assert snapshot.quarantined == 1

    clock.advance(31)
    snapshot = pool.snapshot()
    assert snapshot.available == 2
    assert snapshot.cooling_down == 0
    assert snapshot.quarantined == 1


def test_retry_after_parses_seconds_http_date_and_fallback() -> None:
    now = datetime.now(timezone.utc)
    future = format_datetime(now + timedelta(seconds=45), usegmt=True)

    assert parse_retry_after_seconds("30", default_seconds=60, now=now) == 30
    assert 44 <= parse_retry_after_seconds(future, default_seconds=60, now=now) <= 45
    assert parse_retry_after_seconds("invalid", default_seconds=60, now=now) == 60
    assert parse_retry_after_seconds(None, default_seconds=60, now=now) == 60


@pytest.mark.parametrize("status", [401, 403, 408, 429])
def test_key_specific_statuses_allow_failover(status: int) -> None:
    pool = _pool()
    assert pool.should_failover(status, five_xx_failovers=0) is True


def test_5xx_failover_is_bounded_and_request_errors_do_not_rotate() -> None:
    pool = _pool()

    assert pool.should_failover(500, five_xx_failovers=0) is True
    assert pool.should_failover(502, five_xx_failovers=1) is False
    assert pool.should_failover(400, five_xx_failovers=0) is False
    assert pool.should_failover(404, five_xx_failovers=0) is False
    assert pool.should_failover(422, five_xx_failovers=0) is False


def test_attempted_fingerprints_are_never_selected_again() -> None:
    pool = _pool(("key-one", "key-two"))
    attempted = frozenset(
        {
            fingerprint_secret("key-one"),
            fingerprint_secret("key-two"),
        }
    )

    with pytest.raises(PoolUnavailableError, match="no untried NVIDIA key"):
        pool.acquire(attempted)


def test_pool_queue_capacity_and_wait_timeout() -> None:
    pool = _pool(
        ("key-one",),
        queue_wait_seconds=0.1,
        max_total_queued=1,
    )
    active = pool.acquire(frozenset())
    release_waiter = Event()

    def wait_for_key() -> None:
        try:
            with pool.acquire(frozenset()) as lease:
                release_waiter.wait(timeout=2)
                lease.set_result(status=200, retry_after=None)
        except PoolWaitTimeoutError:
            return

    waiter = Thread(target=wait_for_key)
    waiter.start()
    _wait_until(lambda: pool.snapshot().queued == 1)

    with pytest.raises(PoolQueueFullError):
        pool.acquire(frozenset())

    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert pool.snapshot().queued == 0
    active.release(status=200, retry_after=None)


def test_pool_wait_timeout_is_reported() -> None:
    pool = _pool(("key-one",), queue_wait_seconds=0.05)
    active = pool.acquire(frozenset())

    with pytest.raises(PoolWaitTimeoutError):
        pool.acquire(frozenset())

    active.release(status=200, retry_after=None)


def test_release_is_idempotent_and_snapshot_never_contains_key_material() -> None:
    pool = _pool(("very-secret-nvidia-key",))
    lease = pool.acquire(frozenset())
    lease.release(status=200, retry_after=None)
    lease.release(status=200, retry_after=None)

    snapshot_text = repr(pool.snapshot())
    assert "very-secret-nvidia-key" not in snapshot_text
    assert fingerprint_secret("very-secret-nvidia-key") not in snapshot_text
    assert pool.snapshot().active == 0
