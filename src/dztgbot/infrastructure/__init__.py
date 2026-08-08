"""Infrastructure layer package containing persistence adapters, gateways, and external integrations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import secrets
from typing import Any
import uuid

from .gemini_gateway import GeminiGateway, GeminiGatewayError, PromptBudgets
from .jira_gateway import JiraGateway, JiraGatewayError, JiraTimeouts
from .persistence.workflow_sqlite import SQLiteWorkflowRepository

LOGGER = logging.getLogger(__name__)


class SystemClock:
    """System clock implementing ClockPort."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UuidIdGenerator:
    """UUID and secure token generator implementing IdGeneratorPort."""

    def generate_uuid(self) -> str:
        return str(uuid.uuid4())

    def generate_opaque_token(self, length_bytes: int = 16) -> str:
        return secrets.token_urlsafe(length_bytes)


class AsyncTaskScheduler:
    """Asyncio-backed task scheduler implementing TaskSchedulerPort."""

    def __init__(self) -> None:
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def schedule_timer(
        self,
        job_id: str,
        delay_seconds: float,
        callback: Any,
    ) -> None:
        self.cancel_timer(job_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _runner() -> None:
            self._timers.pop(job_id, None)
            try:
                res = callback()
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    task = loop.create_task(res)
                    self._tasks[job_id] = task
                    task.add_done_callback(lambda _: self._tasks.pop(job_id, None))
            except Exception as exc:
                LOGGER.error("Error running timer callback %s: %s", job_id, exc)

        if delay_seconds <= 0:
            loop.call_soon(_runner)
        else:
            handle = loop.call_later(delay_seconds, _runner)
            self._timers[job_id] = handle

    def cancel_timer(self, job_id: str) -> bool:
        cancelled = False
        handle = self._timers.pop(job_id, None)
        if handle is not None:
            handle.cancel()
            cancelled = True
        task = self._tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
            cancelled = True
        return cancelled

    async def close(self) -> None:
        """Cancel all pending timers and tasks at shutdown."""
        for handle in list(self._timers.values()):
            handle.cancel()
        self._timers.clear()

        pending_tasks = list(self._tasks.values())
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._tasks.clear()


__all__ = [
    "AsyncTaskScheduler",
    "GeminiGateway",
    "GeminiGatewayError",
    "JiraGateway",
    "JiraGatewayError",
    "JiraTimeouts",
    "PromptBudgets",
    "SQLiteWorkflowRepository",
    "SystemClock",
    "UuidIdGenerator",
]
