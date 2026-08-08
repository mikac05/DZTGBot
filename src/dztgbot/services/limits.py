"""Provider-neutral resource limits, deadlines, retries, and cooldowns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import time
from typing import Generic, TypeVar


T = TypeVar("T")

SAFE_OVERLOAD_FEEDBACK = "The service is busy. Please try again shortly."
SAFE_DEADLINE_FEEDBACK = "The operation took too long. Please try again."
SAFE_COOLDOWN_FEEDBACK = "The service is temporarily unavailable. Please try again shortly."
SAFE_CLOSED_FEEDBACK = "The service is stopping. Please try again later."


class ResourceKind(StrEnum):
    GEMINI = "gemini"
    JIRA = "jira"
    ATTACHMENT = "attachment"


@dataclass(frozen=True, slots=True)
class ResourceLimitSpec:
    global_limit: int
    per_actor_limit: int
    queue_limit: int
    total_deadline_seconds: float
    retry_budget: int = 0
    cooldown_failure_threshold: int = 3
    cooldown_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.global_limit <= 0 or self.per_actor_limit <= 0:
            raise ValueError("concurrency limits must be positive")
        if self.per_actor_limit > self.global_limit:
            raise ValueError("per_actor_limit cannot exceed global_limit")
        if self.queue_limit < 0:
            raise ValueError("queue_limit must not be negative")
        if self.total_deadline_seconds <= 0:
            raise ValueError("total_deadline_seconds must be positive")
        if self.retry_budget < 0:
            raise ValueError("retry_budget must not be negative")
        if self.cooldown_failure_threshold <= 0 or self.cooldown_seconds < 0:
            raise ValueError("cooldown settings are invalid")


class LimitOutcome(StrEnum):
    COMPLETED = "completed"
    OVERLOADED = "overloaded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    COOLDOWN = "cooldown"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class LimitResult(Generic[T]):
    outcome: LimitOutcome
    value: T | None = None
    attempts: int = 0
    feedback: str | None = None

    @property
    def completed(self) -> bool:
        return self.outcome is LimitOutcome.COMPLETED


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    admitted: int
    active: int
    actor_gate_count: int
    consecutive_failures: int
    cooling_down: bool


class ResourceControlError(RuntimeError):
    code = "resource_error"
    feedback = SAFE_OVERLOAD_FEEDBACK

    def __init__(self, *, attempts: int = 0) -> None:
        self.attempts = attempts
        super().__init__(self.code)


class ResourceOverloadedError(ResourceControlError):
    code = "resource_overloaded"


class ResourceDeadlineExceededError(ResourceControlError):
    code = "resource_deadline_exceeded"
    feedback = SAFE_DEADLINE_FEEDBACK


class ResourceCooldownError(ResourceControlError):
    code = "resource_cooldown"
    feedback = SAFE_COOLDOWN_FEEDBACK


class ResourceLimiterClosedError(ResourceControlError):
    code = "resource_limiter_closed"
    feedback = SAFE_CLOSED_FEEDBACK


class _OperationFailure(Exception):
    """Keep operation-raised TimeoutError distinct from our total timeout."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(type(error).__name__)


@dataclass(slots=True)
class _ActorGate:
    semaphore: asyncio.BoundedSemaphore
    references: int = 0


@dataclass(slots=True)
class _ResourceState:
    spec: ResourceLimitSpec
    global_semaphore: asyncio.BoundedSemaphore
    actor_gates: dict[int, _ActorGate] = field(default_factory=dict)
    admitted: int = 0
    active: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0


RetryPredicate = Callable[[Exception], bool]
BackoffPolicy = Callable[[int], float]


class ResourceLimiter:
    """Bound provider resources without importing concrete provider adapters.

    Per-actor slots are acquired before global slots so requests from one actor
    cannot occupy the whole global pool while merely waiting for that actor's
    own limit.  Only semaphores are held during external work; bookkeeping
    locks are always released first.
    """

    def __init__(
        self,
        specs: Mapping[ResourceKind, ResourceLimitSpec],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        missing = set(ResourceKind) - set(specs)
        if missing:
            raise ValueError("a limit specification is required for every resource kind")
        self._states = {
            kind: _ResourceState(spec, asyncio.BoundedSemaphore(spec.global_limit))
            for kind, spec in specs.items()
        }
        self._monotonic = monotonic
        self._state_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._closed = False

    async def run(
        self,
        kind: ResourceKind,
        actor_id: int,
        operation: Callable[[int], Awaitable[T]],
        *,
        retry_if: RetryPredicate | None = None,
        retry_budget: int | None = None,
        backoff: BackoffPolicy | None = None,
        total_deadline_seconds: float | None = None,
    ) -> tuple[T, int]:
        """Execute within bounds and return ``(value, attempts)``.

        Retry budgets count additional dispatches.  A caller may reduce, but
        never increase, the configured budget.  The single total deadline
        includes queueing, operation time, and retry backoff.
        """

        if not isinstance(kind, ResourceKind):
            raise TypeError("kind must be a ResourceKind")
        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        if not callable(operation):
            raise TypeError("operation must be callable")

        state = self._states[kind]
        budget = state.spec.retry_budget if retry_budget is None else retry_budget
        if budget < 0 or budget > state.spec.retry_budget:
            raise ValueError("retry_budget must be within the configured budget")
        deadline = (
            state.spec.total_deadline_seconds
            if total_deadline_seconds is None
            else total_deadline_seconds
        )
        if deadline <= 0 or deadline > state.spec.total_deadline_seconds:
            raise ValueError("deadline must be positive and within the configured deadline")
        retry_predicate = retry_if or (lambda _error: False)
        backoff_policy = backoff or (lambda _attempt: 0.0)

        await self._admit(state)
        attempts = 0
        try:
            try:
                async with asyncio.timeout(deadline):
                    while True:
                        attempts += 1
                        actor_gate, actor_acquired, global_acquired = await self._acquire(
                            state, actor_id
                        )
                        try:
                            value = await operation(attempts)
                        except Exception as error:
                            retryable = retry_predicate(error)
                            await self._record_completion(
                                state,
                                succeeded=False,
                                retryable=retryable,
                            )
                            if not retryable or attempts > budget:
                                raise _OperationFailure(error) from error
                        else:
                            await self._record_completion(state, succeeded=True, retryable=False)
                            return value, attempts
                        finally:
                            await self._release(
                                state,
                                actor_id,
                                actor_gate,
                                actor_acquired=actor_acquired,
                                global_acquired=global_acquired,
                            )

                        delay = backoff_policy(attempts)
                        if delay < 0:
                            raise ValueError("backoff must not be negative")
                        if delay:
                            await asyncio.sleep(delay)
            except _OperationFailure as failure:
                raise failure.error
            except TimeoutError as error:
                raise ResourceDeadlineExceededError(attempts=attempts) from error
        finally:
            await self._finish(state)

    async def try_run(
        self,
        kind: ResourceKind,
        actor_id: int,
        operation: Callable[[int], Awaitable[T]],
        *,
        retry_if: RetryPredicate | None = None,
        retry_budget: int | None = None,
        backoff: BackoffPolicy | None = None,
        total_deadline_seconds: float | None = None,
    ) -> LimitResult[T]:
        """Map control states to fixed feedback; operation errors still propagate."""

        try:
            value, attempts = await self.run(
                kind,
                actor_id,
                operation,
                retry_if=retry_if,
                retry_budget=retry_budget,
                backoff=backoff,
                total_deadline_seconds=total_deadline_seconds,
            )
        except ResourceOverloadedError as error:
            return LimitResult(
                LimitOutcome.OVERLOADED,
                attempts=error.attempts,
                feedback=error.feedback,
            )
        except ResourceDeadlineExceededError as error:
            return LimitResult(
                LimitOutcome.DEADLINE_EXCEEDED,
                attempts=error.attempts,
                feedback=error.feedback,
            )
        except ResourceCooldownError as error:
            return LimitResult(
                LimitOutcome.COOLDOWN,
                attempts=error.attempts,
                feedback=error.feedback,
            )
        except ResourceLimiterClosedError as error:
            return LimitResult(
                LimitOutcome.CLOSED,
                attempts=error.attempts,
                feedback=error.feedback,
            )
        return LimitResult(LimitOutcome.COMPLETED, value=value, attempts=attempts)

    async def snapshot(self, kind: ResourceKind) -> ResourceSnapshot:
        async with self._state_lock:
            state = self._states[kind]
            self._reset_expired_cooldown_locked(state)
            return ResourceSnapshot(
                admitted=state.admitted,
                active=state.active,
                actor_gate_count=len(state.actor_gates),
                consecutive_failures=state.consecutive_failures,
                cooling_down=state.cooldown_until > self._monotonic(),
            )

    async def close(self) -> None:
        """Reject new work and wait for all admitted calls and retries to drain."""

        async with self._state_lock:
            self._closed = True
            if all(state.admitted == 0 for state in self._states.values()):
                self._drained.set()
        await self._drained.wait()

    async def _admit(self, state: _ResourceState) -> None:
        async with self._state_lock:
            if self._closed:
                raise ResourceLimiterClosedError()
            self._reset_expired_cooldown_locked(state)
            if state.cooldown_until > self._monotonic():
                raise ResourceCooldownError()
            capacity = state.spec.global_limit + state.spec.queue_limit
            if state.admitted >= capacity:
                raise ResourceOverloadedError()
            state.admitted += 1
            self._drained.clear()

    async def _acquire(
        self, state: _ResourceState, actor_id: int
    ) -> tuple[_ActorGate, bool, bool]:
        async with self._state_lock:
            actor_gate = state.actor_gates.get(actor_id)
            if actor_gate is None:
                actor_gate = _ActorGate(
                    asyncio.BoundedSemaphore(state.spec.per_actor_limit)
                )
                state.actor_gates[actor_id] = actor_gate
            actor_gate.references += 1

        actor_acquired = False
        global_acquired = False
        try:
            await actor_gate.semaphore.acquire()
            actor_acquired = True
            await state.global_semaphore.acquire()
            global_acquired = True
            async with self._state_lock:
                state.active += 1
            return actor_gate, actor_acquired, global_acquired
        except BaseException:
            if global_acquired:
                state.global_semaphore.release()
            if actor_acquired:
                actor_gate.semaphore.release()
            async with self._state_lock:
                actor_gate.references -= 1
                if actor_gate.references == 0 and state.actor_gates.get(actor_id) is actor_gate:
                    del state.actor_gates[actor_id]
            raise

    async def _release(
        self,
        state: _ResourceState,
        actor_id: int,
        actor_gate: _ActorGate,
        *,
        actor_acquired: bool,
        global_acquired: bool,
    ) -> None:
        async with self._state_lock:
            state.active -= 1
        if global_acquired:
            state.global_semaphore.release()
        if actor_acquired:
            actor_gate.semaphore.release()
        async with self._state_lock:
            actor_gate.references -= 1
            if actor_gate.references == 0 and state.actor_gates.get(actor_id) is actor_gate:
                del state.actor_gates[actor_id]

    async def _record_completion(
        self, state: _ResourceState, *, succeeded: bool, retryable: bool
    ) -> None:
        async with self._state_lock:
            if succeeded:
                state.consecutive_failures = 0
                state.cooldown_until = 0.0
            elif retryable:
                state.consecutive_failures += 1
                if state.consecutive_failures >= state.spec.cooldown_failure_threshold:
                    state.cooldown_until = self._monotonic() + state.spec.cooldown_seconds

    async def _finish(self, state: _ResourceState) -> None:
        async with self._state_lock:
            state.admitted -= 1
            if all(item.admitted == 0 for item in self._states.values()):
                self._drained.set()

    def _reset_expired_cooldown_locked(self, state: _ResourceState) -> None:
        if state.cooldown_until and state.cooldown_until <= self._monotonic():
            state.cooldown_until = 0.0
            state.consecutive_failures = 0


__all__ = [
    "LimitOutcome",
    "LimitResult",
    "ResourceControlError",
    "ResourceCooldownError",
    "ResourceDeadlineExceededError",
    "ResourceKind",
    "ResourceLimitSpec",
    "ResourceLimiter",
    "ResourceLimiterClosedError",
    "ResourceOverloadedError",
    "ResourceSnapshot",
    "SAFE_CLOSED_FEEDBACK",
    "SAFE_COOLDOWN_FEEDBACK",
    "SAFE_DEADLINE_FEEDBACK",
    "SAFE_OVERLOAD_FEEDBACK",
]
