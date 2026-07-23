"""Bounded FIFO scheduling for requests that share an NVIDIA API key."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Condition
from types import TracebackType


class QueueFullError(RuntimeError):
    """Raised when a scheduler capacity limit would be exceeded."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"{scope} request queue is full")


class QueueWaitTimeoutError(TimeoutError):
    """Raised when a queued request is not admitted before its deadline."""


@dataclass(frozen=True)
class SchedulerSnapshot:
    """Secret-free aggregate scheduler state."""

    active: int
    queued: int
    lanes: int


@dataclass(frozen=True)
class _Ticket:
    model: str
    enqueued_at: float


@dataclass
class _Lane:
    active: int = 0
    waiters: deque[_Ticket] = field(default_factory=deque)


class QueueLease:
    """An idempotent lease for one admitted scheduler slot."""

    def __init__(
        self,
        *,
        scheduler: RequestScheduler,
        fingerprint: str,
        queue_position: int,
        wait_ms: int,
    ) -> None:
        self._scheduler = scheduler
        self.fingerprint = fingerprint
        self.queue_position = queue_position
        self.wait_ms = wait_ms
        self._released = False

    def release(self) -> None:
        """Release this lease once and wake queued requests."""

        self._scheduler._release(self)

    def __enter__(self) -> QueueLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class RequestScheduler:
    """Serialize requests per key fingerprint while preserving cross-key concurrency."""

    def __init__(
        self,
        *,
        max_concurrent_per_key: int,
        max_queue_per_key: int,
        max_total_queued: int,
        queue_wait_seconds: float,
    ) -> None:
        if max_concurrent_per_key <= 0:
            raise ValueError("max_concurrent_per_key must be greater than 0")
        if max_queue_per_key <= 0:
            raise ValueError("max_queue_per_key must be greater than 0")
        if max_total_queued <= 0:
            raise ValueError("max_total_queued must be greater than 0")
        if queue_wait_seconds <= 0:
            raise ValueError("queue_wait_seconds must be greater than 0")

        self.max_concurrent_per_key = max_concurrent_per_key
        self.max_queue_per_key = max_queue_per_key
        self.max_total_queued = max_total_queued
        self.queue_wait_seconds = queue_wait_seconds
        self._condition = Condition()
        self._lanes: dict[str, _Lane] = {}
        self._queued = 0

    def acquire(self, fingerprint: str, model: str) -> QueueLease:
        """Acquire a slot immediately or wait in the fingerprint's FIFO lane."""

        started_at = time.monotonic()
        deadline = started_at + self.queue_wait_seconds

        with self._condition:
            lane = self._lanes.setdefault(fingerprint, _Lane())
            if (
                lane.active < self.max_concurrent_per_key
                and not lane.waiters
            ):
                lane.active += 1
                return QueueLease(
                    scheduler=self,
                    fingerprint=fingerprint,
                    queue_position=0,
                    wait_ms=0,
                )

            if len(lane.waiters) >= self.max_queue_per_key:
                raise QueueFullError("per-key")
            if self._queued >= self.max_total_queued:
                raise QueueFullError("global")

            ticket = _Ticket(model=model, enqueued_at=started_at)
            lane.waiters.append(ticket)
            self._queued += 1
            queue_position = len(lane.waiters)

            while (
                lane.waiters[0] is not ticket
                or lane.active >= self.max_concurrent_per_key
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_waiter(fingerprint, lane, ticket)
                    raise QueueWaitTimeoutError("request queue wait timed out")
                self._condition.wait(remaining)

            lane.waiters.popleft()
            self._queued -= 1
            lane.active += 1
            return QueueLease(
                scheduler=self,
                fingerprint=fingerprint,
                queue_position=queue_position,
                wait_ms=int((time.monotonic() - started_at) * 1000),
            )

    def snapshot(self) -> SchedulerSnapshot:
        """Return aggregate counters without fingerprints or request content."""

        with self._condition:
            return SchedulerSnapshot(
                active=sum(lane.active for lane in self._lanes.values()),
                queued=self._queued,
                lanes=len(self._lanes),
            )

    def _remove_waiter(
        self,
        fingerprint: str,
        lane: _Lane,
        ticket: _Ticket,
    ) -> None:
        lane.waiters.remove(ticket)
        self._queued -= 1
        self._remove_idle_lane(fingerprint, lane)
        self._condition.notify_all()

    def _release(self, lease: QueueLease) -> None:
        with self._condition:
            if lease._released:
                return

            lane = self._lanes.get(lease.fingerprint)
            if lane is None or lane.active <= 0:
                raise RuntimeError("scheduler lease has no active lane")

            lease._released = True
            lane.active -= 1
            self._remove_idle_lane(lease.fingerprint, lane)
            self._condition.notify_all()

    def _remove_idle_lane(self, fingerprint: str, lane: _Lane) -> None:
        if lane.active == 0 and not lane.waiters:
            self._lanes.pop(fingerprint, None)
