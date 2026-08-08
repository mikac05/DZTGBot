"""Synthetic concurrency benchmarks expressed as deterministic invariants."""

from __future__ import annotations

import asyncio
import unittest

from dztgbot.infrastructure.keyed_processor import KeyedProcessor, WorkKey
from dztgbot.services.limits import ResourceKind, ResourceLimitSpec, ResourceLimiter


class TestPerformanceInvariants(unittest.IsolatedAsyncioTestCase):
    async def test_benchmark_slow_workflow_does_not_delay_independent_workflow(self) -> None:
        """A fast independent key finishes while the synthetic slow key is paused."""

        processor = KeyedProcessor(max_concurrency=4, max_queue_size=16)
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        completions: list[str] = []

        async def slow() -> str:
            slow_started.set()
            await release_slow.wait()
            completions.append("slow")
            return "slow"

        slow_task = asyncio.create_task(
            processor.run(WorkKey.for_workflow("benchmark-slow"), slow)
        )
        await slow_started.wait()

        fast_values = await asyncio.wait_for(
            asyncio.gather(
                *(
                    processor.run(
                        WorkKey.for_workflow(f"benchmark-fast-{index}"),
                        lambda index=index: asyncio.sleep(0, result=index),
                    )
                    for index in range(8)
                )
            ),
            timeout=0.5,
        )
        completions.append("fast-batch")
        self.assertEqual(fast_values, list(range(8)))
        self.assertFalse(slow_task.done())
        self.assertEqual(completions, ["fast-batch"])

        release_slow.set()
        await slow_task
        self.assertEqual(completions, ["fast-batch", "slow"])

    async def test_measured_processor_concurrency_never_exceeds_limit(self) -> None:
        processor = KeyedProcessor(max_concurrency=3, max_queue_size=64)
        active = 0
        measured_maximum = 0

        async def measured(index: int) -> int:
            nonlocal active, measured_maximum
            active += 1
            measured_maximum = max(measured_maximum, active)
            await asyncio.sleep(0.002)
            active -= 1
            return index

        values = await asyncio.gather(
            *(
                processor.run(
                    WorkKey.for_workflow(f"load-{index}"),
                    lambda index=index: measured(index),
                )
                for index in range(30)
            )
        )
        self.assertEqual(values, list(range(30)))
        self.assertEqual(measured_maximum, 3)
        self.assertEqual((await processor.snapshot()).admitted, 0)

    async def test_measured_provider_concurrency_never_exceeds_global_or_actor_limits(self) -> None:
        spec = ResourceLimitSpec(
            global_limit=4,
            per_actor_limit=2,
            queue_limit=64,
            total_deadline_seconds=2.0,
        )
        limiter = ResourceLimiter({kind: spec for kind in ResourceKind})
        global_active = 0
        global_maximum = 0
        resource_active: dict[ResourceKind, int] = {}
        resource_maximum: dict[ResourceKind, int] = {}
        actor_active: dict[tuple[ResourceKind, int], int] = {}
        actor_maximum: dict[tuple[ResourceKind, int], int] = {}

        async def measured(kind: ResourceKind, actor: int, _attempt: int) -> int:
            nonlocal global_active, global_maximum
            global_active += 1
            global_maximum = max(global_maximum, global_active)
            resource_active[kind] = resource_active.get(kind, 0) + 1
            resource_maximum[kind] = max(
                resource_maximum.get(kind, 0), resource_active[kind]
            )
            key = (kind, actor)
            actor_active[key] = actor_active.get(key, 0) + 1
            actor_maximum[key] = max(actor_maximum.get(key, 0), actor_active[key])
            await asyncio.sleep(0.002)
            actor_active[key] -= 1
            resource_active[kind] -= 1
            global_active -= 1
            return actor

        calls = []
        for index in range(36):
            kind = ResourceKind.ATTACHMENT if index % 2 else ResourceKind.JIRA
            actor = (index % 3) + 1
            calls.append(
                limiter.run(
                    kind,
                    actor,
                    lambda attempt, kind=kind, actor=actor: measured(kind, actor, attempt),
                )
            )
        results = await asyncio.gather(*calls)
        self.assertEqual(len(results), 36)
        # The configured bound is per resource; the two independent pools may
        # together reach eight, while every actor/resource gate stays at two.
        self.assertLessEqual(global_maximum, 8)
        self.assertTrue(all(value <= 4 for value in resource_maximum.values()))
        self.assertTrue(all(value <= 2 for value in actor_maximum.values()))
        for kind in (ResourceKind.JIRA, ResourceKind.ATTACHMENT):
            snapshot = await limiter.snapshot(kind)
            self.assertEqual((snapshot.admitted, snapshot.active), (0, 0))


if __name__ == "__main__":
    unittest.main()
