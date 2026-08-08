"""Bounded, keyed serialization for independently progressing workflows.

The processor owns no background tasks.  Each caller drives its own operation,
while a short registry lock protects admission and key lifecycle bookkeeping.
Per-key order is a chain of caller-owned completion futures, so no ``Lock`` is
held while application/provider code runs.  Global execution slots are
acquired only after a caller reaches the head of its key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Generic, TypeVar


T = TypeVar("T")

SAFE_OVERLOAD_FEEDBACK = "The service is busy. Please try again shortly."
SAFE_DEADLINE_FEEDBACK = "The operation took too long. Please try again."
SAFE_CLOSED_FEEDBACK = "The service is stopping. Please try again later."


@dataclass(frozen=True, slots=True, repr=False)
class WorkKey:
    """Opaque serialization key safe to retain in process memory.

    Keys deliberately expose neither Telegram identifiers nor draft IDs in
    their representation.  They are routing aids, never authorization tokens.
    """

    namespace: str
    _digest: bytes

    def __post_init__(self) -> None:
        if self.namespace not in {"collection", "workflow"}:
            raise ValueError("unsupported work-key namespace")
        if len(self._digest) != 16:
            raise ValueError("work-key digest must contain 16 bytes")

    def __repr__(self) -> str:
        return f"WorkKey(namespace={self.namespace!r}, value=<opaque>)"

    @classmethod
    def for_collection(
        cls,
        *,
        actor_id: int,
        chat_id: int,
        message_thread_id: int | None,
    ) -> "WorkKey":
        """Create a stable pre-workflow key for one private collection scope."""

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        if chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if message_thread_id is not None and message_thread_id <= 0:
            raise ValueError("message_thread_id must be positive when present")
        payload = f"{actor_id}:{chat_id}:{message_thread_id or 0}".encode("ascii")
        return cls("collection", _opaque_digest(b"dztgbot-collection-v1", payload))

    @classmethod
    def for_workflow(cls, draft_id: str) -> "WorkKey":
        """Create a stable key after a durable workflow ID exists."""

        normalized = draft_id.strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("draft_id is invalid")
        return cls(
            "workflow",
            _opaque_digest(b"dztgbot-workflow-v1", normalized.encode("utf-8")),
        )


def _opaque_digest(person: bytes, payload: bytes) -> bytes:
    return hashlib.blake2b(payload, digest_size=16, person=person[:16]).digest()


class ProcessingOutcome(StrEnum):
    COMPLETED = "completed"
    OVERLOADED = "overloaded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ProcessingResult(Generic[T]):
    outcome: ProcessingOutcome
    value: T | None = None
    feedback: str | None = None

    @property
    def completed(self) -> bool:
        return self.outcome is ProcessingOutcome.COMPLETED


@dataclass(frozen=True, slots=True)
class ProcessorSnapshot:
    admitted: int
    queued: int
    active: int
    key_count: int
    closed: bool


class ProcessorControlError(RuntimeError):
    """Fixed-code base class for safe processor control failures."""

    code = "processor_error"
    feedback = SAFE_OVERLOAD_FEEDBACK

    def __init__(self) -> None:
        super().__init__(self.code)


class ProcessorOverloadedError(ProcessorControlError):
    code = "processor_overloaded"


class ProcessorDeadlineExceededError(ProcessorControlError):
    code = "processor_deadline_exceeded"
    feedback = SAFE_DEADLINE_FEEDBACK


class ProcessorClosedError(ProcessorControlError):
    code = "processor_closed"
    feedback = SAFE_CLOSED_FEEDBACK


@dataclass(slots=True)
class _KeyEntry:
    tail: asyncio.Future[None]
    references: int = 0


class KeyedProcessor:
    """Serialize each key while allowing bounded progress across other keys.

    ``max_queue_size`` bounds admitted work waiting behind either a key or an
    execution slot.  Total admitted work is therefore bounded by
    ``max_concurrency + max_queue_size``.
    """

    def __init__(self, *, max_concurrency: int, max_queue_size: int) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must not be negative")
        self._max_concurrency = max_concurrency
        self._capacity = max_concurrency + max_queue_size
        self._execution_slots = asyncio.BoundedSemaphore(max_concurrency)
        self._state_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._entries: dict[WorkKey, _KeyEntry] = {}
        self._admitted = 0
        self._active = 0
        self._closed = False

    async def run(
        self,
        key: WorkKey,
        operation: Callable[[], Awaitable[T]],
        *,
        total_deadline_seconds: float | None = None,
    ) -> T:
        """Run one operation, raising only fixed-code control errors.

        Exceptions raised by ``operation`` propagate unchanged.  Callers that
        prefer a safe overload/deadline result can use :meth:`try_run`.
        """

        if not isinstance(key, WorkKey):
            raise TypeError("key must be a WorkKey")
        if not callable(operation):
            raise TypeError("operation must be callable")
        if total_deadline_seconds is not None and total_deadline_seconds <= 0:
            raise ValueError("total_deadline_seconds must be positive")

        entry, predecessor, completion = await self._admit(key)
        turn_acquired = False
        slot_acquired = False
        active = False
        try:
            async def execute() -> T:
                nonlocal turn_acquired, slot_acquired, active
                # Shielding prevents cancellation of a queued caller from
                # cancelling the predecessor's completion signal.
                await asyncio.shield(predecessor)
                turn_acquired = True
                # Same-key waiters do not consume scarce global execution slots.
                await self._execution_slots.acquire()
                slot_acquired = True
                async with self._state_lock:
                    self._active += 1
                    active = True
                return await operation()

            try:
                if total_deadline_seconds is None:
                    return await execute()
                async with asyncio.timeout(total_deadline_seconds):
                    return await execute()
            except TimeoutError as error:
                raise ProcessorDeadlineExceededError() from error
        finally:
            if active:
                async with self._state_lock:
                    self._active -= 1
            if slot_acquired:
                self._execution_slots.release()
            self._complete_turn(
                predecessor,
                completion,
                turn_acquired=turn_acquired,
            )
            await self._finish(key, entry)

    async def try_run(
        self,
        key: WorkKey,
        operation: Callable[[], Awaitable[T]],
        *,
        total_deadline_seconds: float | None = None,
    ) -> ProcessingResult[T]:
        """Return a fixed safe result for admission, deadline, or close states."""

        try:
            value = await self.run(
                key,
                operation,
                total_deadline_seconds=total_deadline_seconds,
            )
        except ProcessorOverloadedError as error:
            return ProcessingResult(ProcessingOutcome.OVERLOADED, feedback=error.feedback)
        except ProcessorDeadlineExceededError as error:
            return ProcessingResult(
                ProcessingOutcome.DEADLINE_EXCEEDED,
                feedback=error.feedback,
            )
        except ProcessorClosedError as error:
            return ProcessingResult(ProcessingOutcome.CLOSED, feedback=error.feedback)
        return ProcessingResult(ProcessingOutcome.COMPLETED, value=value)

    async def snapshot(self) -> ProcessorSnapshot:
        async with self._state_lock:
            return ProcessorSnapshot(
                admitted=self._admitted,
                queued=self._admitted - self._active,
                active=self._active,
                key_count=len(self._entries),
                closed=self._closed,
            )

    async def close(self) -> None:
        """Reject new work and drain every already-admitted caller."""

        async with self._state_lock:
            self._closed = True
            if self._admitted == 0:
                self._drained.set()
        await self._drained.wait()

    async def _admit(
        self, key: WorkKey
    ) -> tuple[_KeyEntry, asyncio.Future[None], asyncio.Future[None]]:
        async with self._state_lock:
            if self._closed:
                raise ProcessorClosedError()
            if self._admitted >= self._capacity:
                raise ProcessorOverloadedError()
            entry = self._entries.get(key)
            if entry is None:
                initial = asyncio.get_running_loop().create_future()
                initial.set_result(None)
                entry = _KeyEntry(initial)
                self._entries[key] = entry
            predecessor = entry.tail
            completion = asyncio.get_running_loop().create_future()
            entry.tail = completion
            entry.references += 1
            self._admitted += 1
            self._drained.clear()
            return entry, predecessor, completion

    @staticmethod
    def _complete_turn(
        predecessor: asyncio.Future[None],
        completion: asyncio.Future[None],
        *,
        turn_acquired: bool,
    ) -> None:
        if completion.done():
            return
        if turn_acquired or predecessor.done():
            completion.set_result(None)
            return

        # A queued caller may time out or be cancelled before its predecessor.
        # Preserve the chain so later same-key work still waits for that
        # predecessor, without spawning an orphan task.
        def complete_after_predecessor(_finished: asyncio.Future[None]) -> None:
            if not completion.done():
                completion.set_result(None)

        predecessor.add_done_callback(complete_after_predecessor)

    async def _finish(self, key: WorkKey, entry: _KeyEntry) -> None:
        async with self._state_lock:
            entry.references -= 1
            self._admitted -= 1
            if entry.references == 0 and self._entries.get(key) is entry:
                del self._entries[key]
            if self._admitted == 0:
                self._drained.set()


__all__ = [
    "KeyedProcessor",
    "ProcessingOutcome",
    "ProcessingResult",
    "ProcessorClosedError",
    "ProcessorControlError",
    "ProcessorDeadlineExceededError",
    "ProcessorOverloadedError",
    "ProcessorSnapshot",
    "SAFE_CLOSED_FEEDBACK",
    "SAFE_DEADLINE_FEEDBACK",
    "SAFE_OVERLOAD_FEEDBACK",
    "WorkKey",
]
