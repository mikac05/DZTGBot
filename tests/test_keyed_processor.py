"""Deterministic tests for bounded keyed update processing."""

from __future__ import annotations

import asyncio
import unittest

from dztgbot.infrastructure.keyed_processor import (
    KeyedProcessor,
    ProcessingOutcome,
    ProcessorClosedError,
    ProcessorOverloadedError,
    WorkKey,
)


class TestWorkKey(unittest.TestCase):
    def test_collection_and_workflow_keys_are_stable_and_opaque(self) -> None:
        first = WorkKey.for_collection(actor_id=123456, chat_id=123456, message_thread_id=None)
        same = WorkKey.for_collection(actor_id=123456, chat_id=123456, message_thread_id=None)
        other = WorkKey.for_collection(actor_id=123456, chat_id=123456, message_thread_id=7)
        workflow = WorkKey.for_workflow("draft-secret-looking-id")

        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, workflow)
        self.assertNotIn("123456", repr(first))
        self.assertNotIn("draft-secret-looking-id", repr(workflow))

    def test_invalid_collection_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkKey.for_collection(actor_id=0, chat_id=1, message_thread_id=None)
        with self.assertRaises(ValueError):
            WorkKey.for_collection(actor_id=1, chat_id=0, message_thread_id=None)
        with self.assertRaises(ValueError):
            WorkKey.for_workflow(" ")


class TestKeyedProcessor(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serial_and_preserves_admission_order(self) -> None:
        processor = KeyedProcessor(max_concurrency=3, max_queue_size=8)
        key = WorkKey.for_workflow("draft-a")
        active = 0
        maximum = 0
        order: list[str] = []

        async def operation(label: str) -> str:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            order.append(f"start:{label}")
            await asyncio.sleep(0)
            order.append(f"end:{label}")
            active -= 1
            return label

        tasks = [
            asyncio.create_task(processor.run(key, lambda label=label: operation(label)))
            for label in ("a", "b", "c")
        ]
        self.assertEqual(await asyncio.gather(*tasks), ["a", "b", "c"])
        self.assertEqual(maximum, 1)
        self.assertEqual(
            order,
            ["start:a", "end:a", "start:b", "end:b", "start:c", "end:c"],
        )
        self.assertEqual((await processor.snapshot()).key_count, 0)

    async def test_slow_key_does_not_block_unrelated_key(self) -> None:
        processor = KeyedProcessor(max_concurrency=2, max_queue_size=4)
        slow_key = WorkKey.for_workflow("slow")
        fast_key = WorkKey.for_workflow("fast")
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()

        async def slow() -> str:
            slow_started.set()
            await release_slow.wait()
            return "slow"

        slow_task = asyncio.create_task(processor.run(slow_key, slow))
        await slow_started.wait()
        result = await asyncio.wait_for(
            processor.run(fast_key, lambda: asyncio.sleep(0, result="fast")),
            timeout=0.2,
        )
        self.assertEqual(result, "fast")
        self.assertFalse(slow_task.done())
        release_slow.set()
        self.assertEqual(await slow_task, "slow")

    async def test_queue_is_bounded_and_overload_feedback_is_fixed(self) -> None:
        processor = KeyedProcessor(max_concurrency=1, max_queue_size=0)
        key = WorkKey.for_workflow("busy")
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked() -> None:
            started.set()
            await release.wait()

        first = asyncio.create_task(processor.run(key, blocked))
        await started.wait()
        with self.assertRaises(ProcessorOverloadedError) as caught:
            await processor.run(key, lambda: asyncio.sleep(0))
        self.assertEqual(str(caught.exception), "processor_overloaded")
        safe = await processor.try_run(key, lambda: asyncio.sleep(0))
        self.assertEqual(safe.outcome, ProcessingOutcome.OVERLOADED)
        self.assertEqual(safe.feedback, "The service is busy. Please try again shortly.")
        release.set()
        await first

    async def test_total_deadline_cleans_key_and_slot(self) -> None:
        processor = KeyedProcessor(max_concurrency=1, max_queue_size=1)
        key = WorkKey.for_workflow("deadline")
        result = await processor.try_run(
            key,
            lambda: asyncio.sleep(1),
            total_deadline_seconds=0.01,
        )
        self.assertEqual(result.outcome, ProcessingOutcome.DEADLINE_EXCEEDED)
        snapshot = await processor.snapshot()
        self.assertEqual((snapshot.admitted, snapshot.active, snapshot.key_count), (0, 0, 0))

    async def test_timed_out_waiter_does_not_break_same_key_chain(self) -> None:
        processor = KeyedProcessor(max_concurrency=2, max_queue_size=3)
        key = WorkKey.for_workflow("ordered-timeout")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        third_started = asyncio.Event()

        async def first_operation() -> None:
            first_started.set()
            await release_first.wait()

        first = asyncio.create_task(processor.run(key, first_operation))
        await first_started.wait()
        second = await processor.try_run(
            key,
            lambda: asyncio.sleep(0),
            total_deadline_seconds=0.01,
        )
        self.assertEqual(second.outcome, ProcessingOutcome.DEADLINE_EXCEEDED)

        async def third_operation() -> None:
            third_started.set()

        third = asyncio.create_task(processor.run(key, third_operation))
        await asyncio.sleep(0.01)
        self.assertFalse(third_started.is_set())
        release_first.set()
        await first
        await third
        self.assertTrue(third_started.is_set())
        self.assertEqual((await processor.snapshot()).key_count, 0)

    async def test_close_drains_admitted_work_and_rejects_new_work(self) -> None:
        processor = KeyedProcessor(max_concurrency=1, max_queue_size=1)
        key = WorkKey.for_workflow("closing")
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked() -> None:
            started.set()
            await release.wait()

        work = asyncio.create_task(processor.run(key, blocked))
        await started.wait()
        closing = asyncio.create_task(processor.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        with self.assertRaises(ProcessorClosedError):
            await processor.run(key, lambda: asyncio.sleep(0))
        release.set()
        await work
        await closing
        snapshot = await processor.snapshot()
        self.assertTrue(snapshot.closed)
        self.assertEqual(snapshot.key_count, 0)


if __name__ == "__main__":
    unittest.main()
