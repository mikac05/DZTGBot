"""Resource-limit, retry, cooldown, and privacy-safe observability tests."""

from __future__ import annotations

import asyncio
import unittest

from dztgbot.services.limits import (
    LimitOutcome,
    ResourceKind,
    ResourceLimitSpec,
    ResourceLimiter,
    ResourceOverloadedError,
)
from dztgbot.services.observability import (
    CorrelationId,
    EventCode,
    OutcomeCode,
    SafeMetrics,
    new_correlation_id,
)


def _spec(
    *,
    global_limit: int = 2,
    per_actor_limit: int = 1,
    queue_limit: int = 4,
    deadline: float = 1.0,
    retries: int = 0,
    threshold: int = 3,
    cooldown: float = 5.0,
) -> ResourceLimitSpec:
    return ResourceLimitSpec(
        global_limit=global_limit,
        per_actor_limit=per_actor_limit,
        queue_limit=queue_limit,
        total_deadline_seconds=deadline,
        retry_budget=retries,
        cooldown_failure_threshold=threshold,
        cooldown_seconds=cooldown,
    )


def _specs(default: ResourceLimitSpec | None = None) -> dict[ResourceKind, ResourceLimitSpec]:
    selected = default or _spec()
    return {kind: selected for kind in ResourceKind}


class TestResourceLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_global_and_per_actor_limits_are_both_enforced(self) -> None:
        limiter = ResourceLimiter(_specs(_spec(global_limit=2, per_actor_limit=1)))
        release = asyncio.Event()
        two_started = asyncio.Event()
        active_global = 0
        maximum_global = 0
        active_by_actor: dict[int, int] = {}
        maximum_by_actor: dict[int, int] = {}

        async def operation(actor_id: int, _attempt: int) -> int:
            nonlocal active_global, maximum_global
            active_global += 1
            maximum_global = max(maximum_global, active_global)
            active_by_actor[actor_id] = active_by_actor.get(actor_id, 0) + 1
            maximum_by_actor[actor_id] = max(
                maximum_by_actor.get(actor_id, 0), active_by_actor[actor_id]
            )
            if active_global == 2:
                two_started.set()
            await release.wait()
            active_by_actor[actor_id] -= 1
            active_global -= 1
            return actor_id

        actors = (1, 1, 2, 3)
        tasks = [
            asyncio.create_task(
                limiter.run(
                    ResourceKind.GEMINI,
                    actor,
                    lambda attempt, actor=actor: operation(actor, attempt),
                )
            )
            for actor in actors
        ]
        await asyncio.wait_for(two_started.wait(), timeout=0.2)
        snapshot = await limiter.snapshot(ResourceKind.GEMINI)
        self.assertEqual(snapshot.active, 2)
        self.assertEqual(maximum_global, 2)
        self.assertTrue(all(value <= 1 for value in maximum_by_actor.values()))
        release.set()
        values = await asyncio.gather(*tasks)
        self.assertEqual(sorted(value for value, _attempts in values), [1, 1, 2, 3])
        self.assertEqual((await limiter.snapshot(ResourceKind.GEMINI)).actor_gate_count, 0)

    async def test_resource_queue_capacity_is_hard_bounded(self) -> None:
        limiter = ResourceLimiter(
            _specs(_spec(global_limit=1, per_actor_limit=1, queue_limit=1))
        )
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocked(_attempt: int) -> None:
            started.set()
            await release.wait()

        first = asyncio.create_task(limiter.run(ResourceKind.JIRA, 1, blocked))
        await started.wait()
        second = asyncio.create_task(
            limiter.run(ResourceKind.JIRA, 2, lambda _attempt: asyncio.sleep(0))
        )
        await asyncio.sleep(0)
        with self.assertRaises(ResourceOverloadedError):
            await limiter.run(ResourceKind.JIRA, 3, lambda _attempt: asyncio.sleep(0))
        result = await limiter.try_run(
            ResourceKind.JIRA, 4, lambda _attempt: asyncio.sleep(0)
        )
        self.assertEqual(result.outcome, LimitOutcome.OVERLOADED)
        release.set()
        await first
        await second

    async def test_total_deadline_includes_queue_and_external_work(self) -> None:
        limiter = ResourceLimiter(_specs(_spec(deadline=0.02)))
        result = await limiter.try_run(
            ResourceKind.ATTACHMENT,
            1,
            lambda _attempt: asyncio.sleep(1),
        )
        self.assertEqual(result.outcome, LimitOutcome.DEADLINE_EXCEEDED)
        self.assertEqual(result.attempts, 1)
        snapshot = await limiter.snapshot(ResourceKind.ATTACHMENT)
        self.assertEqual((snapshot.admitted, snapshot.active, snapshot.actor_gate_count), (0, 0, 0))

    async def test_retry_budget_is_exact_and_cannot_be_increased(self) -> None:
        limiter = ResourceLimiter(_specs(_spec(retries=2, threshold=10)))
        attempts: list[int] = []

        async def transient(attempt: int) -> str:
            attempts.append(attempt)
            if attempt < 3:
                raise ConnectionError("provider detail must not enter control results")
            return "ok"

        value, count = await limiter.run(
            ResourceKind.GEMINI,
            7,
            transient,
            retry_if=lambda error: isinstance(error, ConnectionError),
        )
        self.assertEqual((value, count, attempts), ("ok", 3, [1, 2, 3]))
        with self.assertRaises(ValueError):
            await limiter.run(
                ResourceKind.GEMINI,
                7,
                transient,
                retry_budget=3,
            )

    async def test_operation_timeout_error_is_not_confused_with_total_deadline(self) -> None:
        limiter = ResourceLimiter(_specs(_spec(deadline=1.0)))

        async def provider_timeout(_attempt: int) -> None:
            raise TimeoutError("provider timeout classification remains with the adapter")

        with self.assertRaisesRegex(TimeoutError, "provider timeout classification"):
            await limiter.run(ResourceKind.JIRA, 1, provider_timeout)

    async def test_cooldown_recovers_automatically_and_is_not_sticky(self) -> None:
        now = [10.0]
        limiter = ResourceLimiter(
            _specs(_spec(threshold=1, cooldown=5.0)),
            monotonic=lambda: now[0],
        )

        async def fail(_attempt: int) -> None:
            raise ConnectionError("private provider text")

        with self.assertRaises(ConnectionError):
            await limiter.run(
                ResourceKind.JIRA,
                1,
                fail,
                retry_if=lambda error: isinstance(error, ConnectionError),
            )
        cooling = await limiter.try_run(
            ResourceKind.JIRA, 2, lambda _attempt: asyncio.sleep(0, result="blocked")
        )
        self.assertEqual(cooling.outcome, LimitOutcome.COOLDOWN)
        self.assertNotIn("provider", cooling.feedback or "")

        now[0] += 5.0
        recovered = await limiter.try_run(
            ResourceKind.JIRA, 2, lambda _attempt: asyncio.sleep(0, result="ok")
        )
        self.assertEqual((recovered.outcome, recovered.value), (LimitOutcome.COMPLETED, "ok"))
        self.assertFalse((await limiter.snapshot(ResourceKind.JIRA)).cooling_down)

    async def test_close_drains_and_then_returns_fixed_closed_result(self) -> None:
        limiter = ResourceLimiter(_specs())
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocked(_attempt: int) -> None:
            started.set()
            await release.wait()

        work = asyncio.create_task(limiter.run(ResourceKind.ATTACHMENT, 1, blocked))
        await started.wait()
        closing = asyncio.create_task(limiter.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        result = await limiter.try_run(
            ResourceKind.GEMINI, 2, lambda _attempt: asyncio.sleep(0)
        )
        self.assertEqual(result.outcome, LimitOutcome.CLOSED)
        release.set()
        await work
        await closing


class TestSafeMetrics(unittest.TestCase):
    def test_only_fixed_codes_and_opaque_correlation_ids_are_accepted(self) -> None:
        metrics = SafeMetrics(recent_event_limit=2)
        correlation = new_correlation_id()
        metrics.record(EventCode.JIRA_CALL, OutcomeCode.OK, correlation)
        with self.assertRaises(TypeError):
            metrics.record("message body", OutcomeCode.OK, correlation)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            CorrelationId("raw-user-id-123")
        with self.assertRaises(ValueError):
            metrics.record(
                EventCode.JIRA_CALL,
                OutcomeCode.OK,
                correlation,
                duration_seconds=float("nan"),
            )
        self.assertNotIn(correlation.value, repr(correlation))

    def test_counters_timers_and_recent_history_are_bounded(self) -> None:
        ticks = iter((1.0, 1.125, 2.0, 2.25, 3.0, 3.5))
        metrics = SafeMetrics(recent_event_limit=2)
        for outcome in (OutcomeCode.OK, OutcomeCode.OVERLOADED, OutcomeCode.DEADLINE):
            with metrics.timer(
                EventCode.QUEUE_ADMISSION,
                new_correlation_id(),
                monotonic=lambda: next(ticks),
            ) as timer:
                timer.outcome(outcome)

        snapshot = metrics.snapshot()
        self.assertEqual(sum(snapshot.counters.values()), 3)
        self.assertEqual(len(snapshot.recent), 2)
        self.assertEqual(
            snapshot.total_duration_ms[(EventCode.QUEUE_ADMISSION, OutcomeCode.OK)],
            125.0,
        )
        self.assertTrue(
            all(
                isinstance(event, EventCode) and isinstance(outcome, OutcomeCode)
                for event, outcome in snapshot.counters
            )
        )


if __name__ == "__main__":
    unittest.main()
