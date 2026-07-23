from __future__ import annotations

import time
from threading import Event, Thread
from typing import Callable

import pytest

from nvidia_nim_proxy.scheduler import (
    QueueFullError,
    QueueWaitTimeoutError,
    RequestScheduler,
)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def _scheduler(
    *,
    max_queue_per_key: int = 4,
    max_total_queued: int = 32,
    queue_wait_seconds: float = 2.0,
) -> RequestScheduler:
    return RequestScheduler(
        max_concurrent_per_key=1,
        max_queue_per_key=max_queue_per_key,
        max_total_queued=max_total_queued,
        queue_wait_seconds=queue_wait_seconds,
    )


def test_same_key_is_fifo() -> None:
    scheduler = _scheduler()
    first = scheduler.acquire("fingerprint-a", "model-a")
    order: list[str] = []

    def acquire_record_release(name: str) -> None:
        with scheduler.acquire("fingerprint-a", "model-a"):
            order.append(name)

    second = Thread(target=acquire_record_release, args=("second",))
    third = Thread(target=acquire_record_release, args=("third",))
    second.start()
    _wait_until(lambda: scheduler.snapshot().queued == 1)
    third.start()
    _wait_until(lambda: scheduler.snapshot().queued == 2)

    first.release()
    second.join(timeout=2)
    third.join(timeout=2)

    assert not second.is_alive()
    assert not third.is_alive()
    assert order == ["second", "third"]
    assert scheduler.snapshot().active == 0
    assert scheduler.snapshot().queued == 0


def test_different_keys_can_be_active_concurrently() -> None:
    scheduler = _scheduler()

    first = scheduler.acquire("fingerprint-a", "model-a")
    second = scheduler.acquire("fingerprint-b", "model-b")

    snapshot = scheduler.snapshot()
    assert snapshot.active == 2
    assert snapshot.queued == 0
    assert snapshot.lanes == 2

    first.release()
    second.release()
    assert scheduler.snapshot().lanes == 0


def test_per_key_queue_capacity_is_enforced() -> None:
    scheduler = _scheduler(max_queue_per_key=1)
    active = scheduler.acquire("fingerprint-a", "model-a")
    admitted = Event()
    release_waiter = Event()

    def wait_for_key() -> None:
        with scheduler.acquire("fingerprint-a", "model-a"):
            admitted.set()
            release_waiter.wait(timeout=2)

    waiter = Thread(target=wait_for_key)
    waiter.start()
    _wait_until(lambda: scheduler.snapshot().queued == 1)

    with pytest.raises(QueueFullError, match="per-key"):
        scheduler.acquire("fingerprint-a", "model-a")

    active.release()
    assert admitted.wait(timeout=2)
    release_waiter.set()
    waiter.join(timeout=2)


def test_global_queue_capacity_is_enforced() -> None:
    scheduler = _scheduler(max_queue_per_key=2, max_total_queued=1)
    active_a = scheduler.acquire("fingerprint-a", "model-a")
    active_b = scheduler.acquire("fingerprint-b", "model-b")
    release_waiter = Event()

    def wait_for_a() -> None:
        with scheduler.acquire("fingerprint-a", "model-a"):
            release_waiter.wait(timeout=2)

    waiter = Thread(target=wait_for_a)
    waiter.start()
    _wait_until(lambda: scheduler.snapshot().queued == 1)

    with pytest.raises(QueueFullError, match="global"):
        scheduler.acquire("fingerprint-b", "model-b")

    active_a.release()
    release_waiter.set()
    waiter.join(timeout=2)
    active_b.release()


def test_queue_wait_timeout_removes_ticket_and_idle_lane() -> None:
    scheduler = _scheduler(queue_wait_seconds=0.05)
    active = scheduler.acquire("fingerprint-a", "model-a")

    with pytest.raises(QueueWaitTimeoutError):
        scheduler.acquire("fingerprint-a", "model-a")

    snapshot = scheduler.snapshot()
    assert snapshot.active == 1
    assert snapshot.queued == 0
    assert snapshot.lanes == 1

    active.release()
    assert scheduler.snapshot().lanes == 0


def test_release_is_idempotent_and_context_manager_releases_on_error() -> None:
    scheduler = _scheduler()
    lease = scheduler.acquire("fingerprint-a", "model-a")
    lease.release()
    lease.release()

    with pytest.raises(RuntimeError, match="boom"):
        with scheduler.acquire("fingerprint-b", "model-b"):
            raise RuntimeError("boom")

    assert scheduler.snapshot().active == 0
    assert scheduler.snapshot().queued == 0
    assert scheduler.snapshot().lanes == 0


def test_lease_reports_queue_position_and_wait_time() -> None:
    scheduler = _scheduler()
    immediate = scheduler.acquire("fingerprint-a", "model-a")
    assert immediate.queue_position == 0
    assert immediate.wait_ms >= 0
    immediate.release()
