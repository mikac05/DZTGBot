"""Concurrency tests for isolated intake batches and out-of-order analysis."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.models import (
    Attachment,
    Draft,
    JiraTaskTemplate,
    MediaKind,
    SourceMessageRef,
)
from dztgbot.infrastructure.persistence.workflow_sqlite import (
    SQLiteWorkflowRepository,
)
from dztgbot.services.intake_service import (
    CollectionReceipt,
    DuplicateMessageError,
    IntakeService,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class IncrementingIds:
    def __init__(self) -> None:
        self.value = 0

    def generate_uuid(self) -> str:
        self.value += 1
        return f"concurrent-draft-{self.value}"

    def generate_opaque_token(self, length_bytes: int = 16) -> str:
        return "b" * (length_bytes * 2)


class FixedRules:
    async def get_rules(self) -> str:
        return "rules"

    async def update_rules(self, new_rules_text: str) -> None:
        raise AssertionError("unexpected rules mutation")


class ManualScheduler:
    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[], Awaitable[Draft | None]]] = {}
        self.active: set[str] = set()

    def schedule_timer(
        self,
        job_id: str,
        delay_seconds: float,
        callback: Callable[[], Awaitable[Draft | None]],
    ) -> None:
        self.callbacks[job_id] = callback
        self.active.add(job_id)

    def cancel_timer(self, job_id: str) -> bool:
        existed = job_id in self.active
        self.active.discard(job_id)
        return existed

    async def fire(self, job_id: str) -> Draft | None:
        if job_id not in self.active:
            return None
        self.active.remove(job_id)
        return await self.callbacks[job_id]()


def template_for(message_id: int) -> JiraTaskTemplate:
    return JiraTaskTemplate(
        project_key="BOT",
        issue_type="Task",
        summary=f"template-{message_id}",
        description=f"description-{message_id}",
        priority="Medium",
    )


class ImmediateAnalyzer:
    def __init__(self) -> None:
        self.call_count = 0

    async def analyze_messages(
        self,
        messages: Sequence[SourceMessageRef],
        rules_text: str,
        default_project_key: str,
    ) -> JiraTaskTemplate:
        self.call_count += 1
        return template_for(messages[0].message_id)


class ControlledAnalyzer:
    def __init__(self) -> None:
        self.started: dict[int, asyncio.Event] = {}
        self.results: dict[int, asyncio.Future[JiraTaskTemplate]] = {}

    def _started_event(self, message_id: int) -> asyncio.Event:
        return self.started.setdefault(message_id, asyncio.Event())

    def _result(self, message_id: int) -> asyncio.Future[JiraTaskTemplate]:
        future = self.results.get(message_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self.results[message_id] = future
        return future

    async def analyze_messages(
        self,
        messages: Sequence[SourceMessageRef],
        rules_text: str,
        default_project_key: str,
    ) -> JiraTaskTemplate:
        message_id = messages[0].message_id
        self._started_event(message_id).set()
        return await self._result(message_id)

    async def wait_until_started(self, message_id: int) -> None:
        await self._started_event(message_id).wait()

    def complete(self, message_id: int) -> None:
        self._result(message_id).set_result(template_for(message_id))


def source(
    message_id: int,
    *,
    chat_id: int = 100,
    media_kind: MediaKind = MediaKind.TEXT,
) -> SourceMessageRef:
    return SourceMessageRef(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=message_id + 1000,
        text=f"message-{message_id}",
        media_kind=media_kind,
        received_at=NOW,
    )


def attachment(message_id: int) -> Attachment:
    return Attachment(
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
        media_kind=MediaKind.PHOTO,
        file_name=f"{message_id}.jpg",
        file_size=100,
    )


class BatchConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "batch.sqlite3"
        self.repository = SQLiteWorkflowRepository(
            self.database_path, enable_wal=False
        )
        self.scheduler = ManualScheduler()
        self.ids = IncrementingIds()

    async def asyncSetUp(self) -> None:
        await self.repository.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_service(
        self,
        analyzer: object,
        *,
        on_draft_ready: Callable[[Draft], Awaitable[None]] | None = None,
    ) -> IntakeService:
        return IntakeService(
            repository=self.repository,
            analyzer=analyzer,  # type: ignore[arg-type]
            rules_repository=FixedRules(),
            scheduler=self.scheduler,
            clock=FixedClock(),
            id_generator=self.ids,
            default_project_key="BOT",
            on_draft_ready=on_draft_ready,
        )

    async def collect(
        self,
        service: IntakeService,
        message_id: int,
        *,
        chat_id: int = 100,
        thread_id: int | None = None,
        with_photo: bool = False,
    ) -> CollectionReceipt:
        return await service.collect_message(
            owner_id=10,
            chat_id=chat_id,
            message_thread_id=thread_id,
            message=source(
                message_id,
                chat_id=chat_id,
                media_kind=MediaKind.PHOTO if with_photo else MediaKind.TEXT,
            ),
            attachment=attachment(message_id) if with_photo else None,
        )

    async def test_out_of_order_analyses_update_only_their_sealed_drafts(self) -> None:
        analyzer = ControlledAnalyzer()
        service = self.make_service(analyzer)

        first = await self.collect(service, 1, with_photo=True)
        first_task = asyncio.create_task(
            self.scheduler.fire(first.deadline_job_id)
        )
        await analyzer.wait_until_started(1)

        second = await self.collect(service, 2, with_photo=True)
        self.assertNotEqual(first.draft_id, second.draft_id)
        second_task = asyncio.create_task(
            self.scheduler.fire(second.deadline_job_id)
        )
        await analyzer.wait_until_started(2)

        analyzer.complete(2)
        second_result = await second_task
        analyzer.complete(1)
        first_result = await first_task

        self.assertEqual(second_result.template.summary, "template-2")  # type: ignore[union-attr]
        self.assertEqual(first_result.template.summary, "template-1")  # type: ignore[union-attr]
        durable_first = await self.repository.get_by_id(first.draft_id)
        durable_second = await self.repository.get_by_id(second.draft_id)
        assert durable_first is not None and durable_second is not None
        self.assertEqual(durable_first.template.summary, "template-1")  # type: ignore[union-attr]
        self.assertEqual(durable_second.template.summary, "template-2")  # type: ignore[union-attr]
        self.assertEqual(durable_first.attachments, (attachment(1),))
        self.assertEqual(durable_second.attachments, (attachment(2),))
        self.assertEqual(
            [item.message_id for item in durable_first.source_messages], [1]
        )
        self.assertEqual(
            [item.message_id for item in durable_second.source_messages], [2]
        )

    async def test_owner_chat_and_thread_scopes_never_share_batches(self) -> None:
        analyzer = ImmediateAnalyzer()
        service = self.make_service(analyzer)
        receipts = (
            await self.collect(service, 10, chat_id=100, thread_id=1, with_photo=True),
            await self.collect(service, 10, chat_id=100, thread_id=2, with_photo=True),
            await self.collect(service, 10, chat_id=200, thread_id=None, with_photo=True),
        )
        self.assertEqual(len({item.draft_id for item in receipts}), 3)

        await asyncio.gather(
            *(self.scheduler.fire(item.deadline_job_id) for item in receipts)
        )
        drafts = [
            await self.repository.get_by_id(item.draft_id) for item in receipts
        ]
        self.assertEqual(
            [draft.message_thread_id for draft in drafts],  # type: ignore[union-attr]
            [1, 2, None],
        )
        self.assertEqual(
            [draft.chat_id for draft in drafts],  # type: ignore[union-attr]
            [100, 100, 200],
        )
        self.assertTrue(
            all(
                len(draft.attachments) == 1  # type: ignore[union-attr]
                for draft in drafts
            )
        )

    async def test_collection_lock_is_free_during_analysis_and_observer_io(self) -> None:
        analyzer = ControlledAnalyzer()
        observer_started = asyncio.Event()
        release_observer = asyncio.Event()
        service_box: list[IntakeService] = []

        async def blocking_observer(draft: Draft) -> None:
            self.assertFalse(service_box[0]._collection_lock.locked())
            observer_started.set()
            await release_observer.wait()

        service = self.make_service(analyzer, on_draft_ready=blocking_observer)
        service_box.append(service)
        first = await self.collect(service, 20)
        first_task = asyncio.create_task(
            self.scheduler.fire(first.deadline_job_id)
        )
        await analyzer.wait_until_started(20)

        second = await asyncio.wait_for(self.collect(service, 21), timeout=0.2)
        self.assertFalse(service._collection_lock.locked())
        analyzer.complete(20)
        await observer_started.wait()

        third = await asyncio.wait_for(self.collect(service, 22), timeout=0.2)
        self.assertEqual(second.draft_id, third.draft_id)
        release_observer.set()
        await first_task
        await service.cancel_pending(
            owner_id=10, chat_id=100, message_thread_id=None
        )

    async def test_concurrent_duplicate_and_concurrent_flush_each_have_one_winner(self) -> None:
        analyzer = ImmediateAnalyzer()
        service = self.make_service(analyzer)
        duplicate_results = await asyncio.gather(
            self.collect(service, 30),
            self.collect(service, 30),
            return_exceptions=True,
        )
        receipts = [
            result for result in duplicate_results if isinstance(result, CollectionReceipt)
        ]
        duplicates = [
            result for result in duplicate_results if isinstance(result, DuplicateMessageError)
        ]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(duplicates), 1)
        receipt = receipts[0]

        timer_result, explicit_result = await asyncio.gather(
            self.scheduler.fire(receipt.deadline_job_id),
            service.flush_scope(
                owner_id=10,
                chat_id=100,
                message_thread_id=None,
            ),
        )
        self.assertEqual(
            sum(result is not None for result in (timer_result, explicit_result)),
            1,
        )
        self.assertEqual(analyzer.call_count, 1)
        durable = await self.repository.get_by_id(receipt.draft_id)
        assert durable is not None
        self.assertEqual(len(durable.source_messages), 1)


if __name__ == "__main__":
    unittest.main()
