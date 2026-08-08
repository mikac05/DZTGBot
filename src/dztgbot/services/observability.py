"""Bounded, privacy-safe counters and timers for application services."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import re
import secrets
import math
from threading import Lock
import time
from types import TracebackType
from typing import Self


_CORRELATION_PATTERN = re.compile(r"^c1_[A-Za-z0-9_-]{16,64}$")


class EventCode(StrEnum):
    KEYED_PROCESS = "keyed_process"
    GEMINI_CALL = "gemini_call"
    JIRA_CALL = "jira_call"
    ATTACHMENT_CALL = "attachment_call"
    QUEUE_ADMISSION = "queue_admission"
    SHUTDOWN = "shutdown"


class OutcomeCode(StrEnum):
    OK = "ok"
    ERROR = "error"
    OVERLOADED = "overloaded"
    DEADLINE = "deadline"
    COOLDOWN = "cooldown"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class CorrelationId:
    value: str

    def __post_init__(self) -> None:
        if not _CORRELATION_PATTERN.fullmatch(self.value):
            raise ValueError("correlation ID must be an opaque c1 token")

    def __repr__(self) -> str:
        return "CorrelationId(<opaque>)"


def new_correlation_id() -> CorrelationId:
    """Return a non-semantic identifier suitable for one operation trace."""

    return CorrelationId(f"c1_{secrets.token_urlsafe(18)}")


@dataclass(frozen=True, slots=True)
class Observation:
    event: EventCode
    outcome: OutcomeCode
    correlation_id: CorrelationId
    duration_ms: float | None


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    counters: dict[tuple[EventCode, OutcomeCode], int]
    total_duration_ms: dict[tuple[EventCode, OutcomeCode], float]
    recent: tuple[Observation, ...]


class SafeMetrics:
    """In-memory metrics with fixed dimensions and a bounded event history.

    The API accepts enums and opaque correlation IDs only: actor/chat IDs,
    tokens, provider bodies, URLs, message text, and arbitrary label maps have
    no entry point.  Aggregate snapshots are safe to export after applying the
    deployment's chosen metrics transport.
    """

    def __init__(self, *, recent_event_limit: int = 256) -> None:
        if recent_event_limit < 0 or recent_event_limit > 10_000:
            raise ValueError("recent_event_limit must be between 0 and 10000")
        self._lock = Lock()
        self._counters: Counter[tuple[EventCode, OutcomeCode]] = Counter()
        self._durations: defaultdict[tuple[EventCode, OutcomeCode], float] = defaultdict(float)
        self._recent: deque[Observation] = deque(maxlen=recent_event_limit)

    def record(
        self,
        event: EventCode,
        outcome: OutcomeCode,
        correlation_id: CorrelationId,
        *,
        duration_seconds: float | None = None,
    ) -> None:
        if not isinstance(event, EventCode) or not isinstance(outcome, OutcomeCode):
            raise TypeError("event and outcome must use fixed code enums")
        if not isinstance(correlation_id, CorrelationId):
            raise TypeError("correlation_id must be opaque")
        if duration_seconds is not None and (
            duration_seconds < 0 or not math.isfinite(duration_seconds)
        ):
            raise ValueError("duration_seconds must be finite and not negative")
        duration_ms = None if duration_seconds is None else duration_seconds * 1000.0
        key = (event, outcome)
        observation = Observation(event, outcome, correlation_id, duration_ms)
        with self._lock:
            self._counters[key] += 1
            if duration_ms is not None:
                self._durations[key] += duration_ms
            self._recent.append(observation)

    def timer(
        self,
        event: EventCode,
        correlation_id: CorrelationId,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "ObservationTimer":
        if not isinstance(event, EventCode):
            raise TypeError("event must use a fixed code enum")
        if not isinstance(correlation_id, CorrelationId):
            raise TypeError("correlation_id must be opaque")
        return ObservationTimer(self, event, correlation_id, monotonic)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(
                counters=dict(self._counters),
                total_duration_ms=dict(self._durations),
                recent=tuple(self._recent),
            )


class ObservationTimer:
    """Synchronous/async context timer with fixed success/error outcomes."""

    def __init__(
        self,
        metrics: SafeMetrics,
        event: EventCode,
        correlation_id: CorrelationId,
        monotonic: Callable[[], float],
    ) -> None:
        self._metrics = metrics
        self._event = event
        self._correlation_id = correlation_id
        self._monotonic = monotonic
        self._started: float | None = None
        self._outcome = OutcomeCode.OK
        self._finished = False

    def outcome(self, value: OutcomeCode) -> None:
        if not isinstance(value, OutcomeCode):
            raise TypeError("outcome must use a fixed code enum")
        self._outcome = value

    def __enter__(self) -> Self:
        if self._started is not None:
            raise RuntimeError("timer cannot be entered more than once")
        self._started = self._monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self._outcome = (
                OutcomeCode.CANCELLED
                if issubclass(exc_type, (asyncio.CancelledError, KeyboardInterrupt))
                else OutcomeCode.ERROR
            )
        self._finish()

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_value, traceback)

    def _finish(self) -> None:
        if self._started is None or self._finished:
            raise RuntimeError("timer is not active")
        duration = max(0.0, self._monotonic() - self._started)
        self._metrics.record(
            self._event,
            self._outcome,
            self._correlation_id,
            duration_seconds=duration,
        )
        self._finished = True


__all__ = [
    "CorrelationId",
    "EventCode",
    "MetricsSnapshot",
    "Observation",
    "ObservationTimer",
    "OutcomeCode",
    "SafeMetrics",
    "new_correlation_id",
]
