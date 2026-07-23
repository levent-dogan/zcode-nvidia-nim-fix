"""Round-robin NVIDIA credential pool with bounded failover state."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Condition
from types import TracebackType
from typing import Callable

from nvidia_nim_proxy.credentials import fingerprint_secret


class PoolUnavailableError(RuntimeError):
    """Raised when no untried NVIDIA key can become available."""


class PoolQueueFullError(RuntimeError):
    """Raised when the bounded pool wait queue is full."""


class PoolWaitTimeoutError(TimeoutError):
    """Raised when a pool request cannot acquire a key before its deadline."""


@dataclass(frozen=True)
class KeyPoolSnapshot:
    """Secret-free aggregate NVIDIA key pool state."""

    total: int
    available: int
    cooling_down: int
    quarantined: int
    active: int
    queued: int


@dataclass
class _KeyState:
    secret: str
    fingerprint: str
    active: int = 0
    cooldown_until: float = 0.0
    quarantined: bool = False


@dataclass(frozen=True)
class _PoolTicket:
    attempted: frozenset[str]
    enqueued_at: float


def parse_retry_after_seconds(
    value: str | None,
    *,
    default_seconds: int,
    now: datetime | None = None,
) -> int:
    """Parse Retry-After seconds or HTTP date with a bounded fallback."""

    if value is None:
        return default_seconds

    normalized = value.strip()
    try:
        return max(0, int(normalized))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return default_seconds

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0, math.ceil((parsed - reference).total_seconds()))


class PoolLease:
    """One active NVIDIA key selection with idempotent result release."""

    def __init__(
        self,
        *,
        pool: NvidiaKeyPool,
        state: _KeyState,
        queue_position: int,
        wait_ms: int,
    ) -> None:
        self._pool = pool
        self._state = state
        self.secret = state.secret
        self.fingerprint = state.fingerprint
        self.queue_position = queue_position
        self.wait_ms = wait_ms
        self._status: int | None = None
        self._retry_after: str | None = None
        self._released = False

    def set_result(self, *, status: int | None, retry_after: str | None) -> None:
        """Record the result that will update pool health on release."""

        self._status = status
        self._retry_after = retry_after

    def release(
        self,
        *,
        status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        """Release the selected key and apply result-specific health state."""

        if status is not None:
            self.set_result(status=status, retry_after=retry_after)
        self._pool._release(self)

    def release_if_active(self) -> None:
        """Release only when this lease has not already been released."""

        self._pool._release(self)

    def __enter__(self) -> PoolLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        return (
            "PoolLease("
            f"fingerprint={self.fingerprint!r}, "
            f"queue_position={self.queue_position}, "
            f"wait_ms={self.wait_ms}, "
            f"released={self._released}"
            ")"
        )


class NvidiaKeyPool:
    """Fairly select healthy keys and track cooldown/quarantine state."""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        max_concurrent_per_key: int,
        max_total_queued: int,
        queue_wait_seconds: float,
        default_cooldown_seconds: int,
        max_5xx_failovers: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not keys:
            raise ValueError("NVIDIA key pool must contain at least one key")
        if max_concurrent_per_key <= 0:
            raise ValueError("max_concurrent_per_key must be greater than 0")
        if max_total_queued <= 0:
            raise ValueError("max_total_queued must be greater than 0")
        if queue_wait_seconds <= 0:
            raise ValueError("queue_wait_seconds must be greater than 0")
        if default_cooldown_seconds <= 0:
            raise ValueError("default_cooldown_seconds must be greater than 0")
        if max_5xx_failovers < 0:
            raise ValueError("max_5xx_failovers must not be negative")

        fingerprints = [fingerprint_secret(key) for key in keys]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("NVIDIA key pool contains duplicate keys")

        self.max_concurrent_per_key = max_concurrent_per_key
        self.max_total_queued = max_total_queued
        self.queue_wait_seconds = queue_wait_seconds
        self.default_cooldown_seconds = default_cooldown_seconds
        self.max_5xx_failovers = max_5xx_failovers
        self._clock = clock
        self._condition = Condition()
        self._states = [
            _KeyState(secret=key, fingerprint=fingerprint)
            for key, fingerprint in zip(keys, fingerprints, strict=True)
        ]
        self._waiters: deque[_PoolTicket] = deque()
        self._cursor = -1

    def acquire(self, attempted: frozenset[str]) -> PoolLease:
        """Select a healthy untried key or wait in the process-wide FIFO queue."""

        started_at = time.monotonic()
        deadline = started_at + self.queue_wait_seconds

        with self._condition:
            if not self._waiters:
                selected = self._select_available(attempted)
                if selected is not None:
                    selected.active += 1
                    return PoolLease(
                        pool=self,
                        state=selected,
                        queue_position=0,
                        wait_ms=0,
                    )
                if self._has_no_future_candidate(attempted):
                    raise PoolUnavailableError("no untried NVIDIA key is available")

            if len(self._waiters) >= self.max_total_queued:
                raise PoolQueueFullError("NVIDIA key pool request queue is full")

            ticket = _PoolTicket(attempted=attempted, enqueued_at=started_at)
            self._waiters.append(ticket)
            queue_position = len(self._waiters)

            while True:
                if self._waiters[0] is ticket:
                    selected = self._select_available(ticket.attempted)
                    if selected is not None:
                        self._waiters.popleft()
                        selected.active += 1
                        return PoolLease(
                            pool=self,
                            state=selected,
                            queue_position=queue_position,
                            wait_ms=int((time.monotonic() - started_at) * 1000),
                        )
                    if self._has_no_future_candidate(ticket.attempted):
                        self._remove_waiter(ticket)
                        raise PoolUnavailableError(
                            "no untried NVIDIA key is available"
                        )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_waiter(ticket)
                    raise PoolWaitTimeoutError("NVIDIA key pool wait timed out")

                self._condition.wait(min(remaining, self._next_state_change_delay(attempted)))

    def should_failover(self, status: int, *, five_xx_failovers: int) -> bool:
        """Return whether pool mode may retry this status with another key."""

        if status in {401, 403, 408, 429}:
            return True
        if status in {500, 502, 503, 504}:
            return five_xx_failovers < self.max_5xx_failovers
        return False

    def snapshot(self) -> KeyPoolSnapshot:
        """Return key health counts without secrets or fingerprints."""

        with self._condition:
            now = self._clock()
            quarantined = sum(state.quarantined for state in self._states)
            cooling_down = sum(
                not state.quarantined and state.cooldown_until > now
                for state in self._states
            )
            available = sum(
                not state.quarantined
                and state.cooldown_until <= now
                and state.active < self.max_concurrent_per_key
                for state in self._states
            )
            return KeyPoolSnapshot(
                total=len(self._states),
                available=available,
                cooling_down=cooling_down,
                quarantined=quarantined,
                active=sum(state.active for state in self._states),
                queued=len(self._waiters),
            )

    def _select_available(self, attempted: frozenset[str]) -> _KeyState | None:
        now = self._clock()
        state_count = len(self._states)
        for offset in range(1, state_count + 1):
            index = (self._cursor + offset) % state_count
            state = self._states[index]
            if (
                state.fingerprint in attempted
                or state.quarantined
                or state.cooldown_until > now
                or state.active >= self.max_concurrent_per_key
            ):
                continue
            self._cursor = index
            return state
        return None

    def _has_no_future_candidate(self, attempted: frozenset[str]) -> bool:
        return all(
            state.fingerprint in attempted or state.quarantined
            for state in self._states
        )

    def _next_state_change_delay(self, attempted: frozenset[str]) -> float:
        now = self._clock()
        delays = [
            state.cooldown_until - now
            for state in self._states
            if state.fingerprint not in attempted
            and not state.quarantined
            and state.cooldown_until > now
        ]
        if not delays:
            return self.queue_wait_seconds
        return max(0.001, min(delays))

    def _remove_waiter(self, ticket: _PoolTicket) -> None:
        self._waiters.remove(ticket)
        self._condition.notify_all()

    def _release(self, lease: PoolLease) -> None:
        with self._condition:
            if lease._released:
                return
            if lease._state.active <= 0:
                raise RuntimeError("pool lease has no active key")

            lease._released = True
            lease._state.active -= 1
            status = lease._status
            if status in {401, 403}:
                lease._state.quarantined = True
            elif status in {408, 429}:
                cooldown_seconds = parse_retry_after_seconds(
                    lease._retry_after,
                    default_seconds=self.default_cooldown_seconds,
                )
                lease._state.cooldown_until = self._clock() + cooldown_seconds

            self._condition.notify_all()
