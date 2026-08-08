"""Unit tests for intake validation, deadlines, and durable batch sealing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import (
    Attachment,
    JiraTaskTemplate,
    MediaKind,
    SourceMessageRef,
)
from dztgbot.infrastructure.persistence.workflow_sqlite import (
    SQLiteWorkflowRepository,
)
from dztgbot.services.intake_service import (
    AttachmentEligibilityError,
    BatchLimitExceededError,
    DuplicateMessageError,
    IntakeService,
    MAX_BATCH_SIZE,
    PromptBudgetExceededError,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def now(self) -> datetime:
        return NOW


class SequenceIdGenerator:
    def __init__(self) -> None:
        self.counter = 0

    def generate_uuid(self) -> str:
        self.counter += 1
        return f"draft-{self.counter}"

    def generate_opaque_token(self, length_bytes: int = 16) -> str:
        return "a" * (length_bytes * 2)


class FakeRulesRepository:
    async def get_rules(self) -> str:
        return "approved-rules"

    async def update_rules(self, new_rules_text: str) -> None:
        raise AssertionError("intake must not update rules")


class ManualScheduler:
    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[], Awaitable[object]]] = {}
        self.active: set[str] = set()
        self.cancelled: set[str] = set()
        self.delays: dict[str, float] = {}

    def schedule_timer(
        self,
        job_id: str,
        delay_seconds: float,
        callback: Callable[[], Awaitable[object]],
    ) -> None:
        self.callbacks[job_id] = callback
        self.active.add(job_id)
        self.delays[job_id] = delay_seconds

    def cancel_timer(self, job_id: str) -> bool:
        existed = job_id in self.active
        self.active.discard(job_id)
        if existed:
            self.cancelled.add(job_id)
        return existed

    async def fire(self, job_id: str, *, include_cancelled: bool = False) -> object:
        if not include_cancelled and job_id not in self.active:
            raise AssertionError("timer is not active")
        self.active.discard(job_id)
        return await self.callbacks[job_id]()


class RecordingAnalyzer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[tuple[SourceMessageRef, ...], str, str]] = []

    async def analyze_messages(
        self,
        messages: Sequence[SourceMessageRef],
        rules_text: str,
        default_project_key: str,
    ) -> JiraTaskTemplate:
        immutable_messages = tuple(messages)
        self.calls.append((immutable_messages, rules_text, default_project_key))
        if self.fail:
            raise RuntimeError("provider payload must not escape")
        return JiraTaskTemplate(
            project_key=default_project_key,
            issue_type="Task",
            summary=f"batch:{immutable_messages[0].message_id}",
            description="analyzed",
            priority="Medium",
        )


def message(
    message_id: int,
    *,
    chat_id: int = 100,
    text: str = "body",
    media_kind: MediaKind = MediaKind.TEXT,
) -> SourceMessageRef:
    return SourceMessageRef(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=900 + message_id,
        text=text,
        media_kind=media_kind,
        received_at=NOW,
    )


def photo_attachment(
    message_id: int, *, file_size: int | None = 100
) -> Attachment:
    return Attachment(
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
        media_kind=MediaKind.PHOTO,
        file_name=f"photo-{message_id}.jpg",
        file_size=file_size,
    )


class IntakeServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "intake.sqlite3"
        self.repository = SQLiteWorkflowRepository(
            self.database_path, enable_wal=False
        )
        self.scheduler = ManualScheduler()
        self.analyzer = RecordingAnalyzer()
        self.ready_drafts = []
        self.failed_drafts = []

    async def asyncSetUp(self) -> None:
        await self.repository.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_service(self, **overrides: object) -> IntakeService:
        arguments: dict[str, object] = {
            "repository": self.repository,
            "analyzer": self.analyzer,
            "rules_repository": FakeRulesRepository(),
            "scheduler": self.scheduler,
            "clock": FakeClock(),
            "id_generator": SequenceIdGenerator(),
            "default_project_key": "BOT",
            "on_draft_ready": self._record_ready,
            "on_draft_failed": self._record_failed,
        }
        arguments.update(overrides)
        return IntakeService(**arguments)  # type: ignore[arg-type]

    async def _record_ready(self, draft: object) -> None:
        self.ready_drafts.append(draft)

    async def _record_failed(self, draft: object) -> None:
        self.failed_drafts.append(draft)

    async def test_deadline_seals_draft_before_analysis_and_persists_result(self) -> None:
        service = self.make_service()
        source = message(1, media_kind=MediaKind.PHOTO, text="photo caption")
        attachment = photo_attachment(1)

        receipt = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=7,
            message=source,
            attachment=attachment,
        )
        self.assertEqual(receipt.batch_size, 1)
        self.assertEqual(self.scheduler.delays[receipt.deadline_job_id], 2.5)
        self.assertEqual(self.analyzer.calls, [])
        self.assertIsNone(await self.repository.get_by_id(receipt.draft_id))

        completed = await self.scheduler.fire(receipt.deadline_job_id)

        self.assertEqual(completed.state, DraftState.REVIEW)  # type: ignore[union-attr]
        durable = await self.repository.get_by_id(receipt.draft_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable.state, DraftState.REVIEW)
        self.assertEqual(durable.revision, 3)
        self.assertEqual(completed.revision, durable.revision)  # type: ignore[union-attr]
        self.assertEqual(durable.source_messages, (source,))
        self.assertEqual(durable.attachments, (attachment,))
        self.assertEqual(durable.template.summary, "batch:1")  # type: ignore[union-attr]
        self.assertEqual(len(self.ready_drafts), 1)
        self.assertEqual(self.analyzer.calls[0][1:], ("approved-rules", "BOT"))

    async def test_duplicate_is_rejected_without_rescheduling_or_mutation(self) -> None:
        service = self.make_service()
        source = message(2)
        first = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=None,
            message=source,
        )

        with self.assertRaisesRegex(DuplicateMessageError, "duplicate_message"):
            await service.collect_message(
                owner_id=10,
                chat_id=100,
                message_thread_id=None,
                message=source,
            )

        self.assertEqual(
            await service.pending_count(
                owner_id=10, chat_id=100, message_thread_id=None
            ),
            1,
        )
        self.assertEqual(self.scheduler.active, {first.deadline_job_id})
        await self.scheduler.fire(first.deadline_job_id)
        with self.assertRaises(DuplicateMessageError):
            await service.collect_message(
                owner_id=10,
                chat_id=100,
                message_thread_id=None,
                message=source,
            )

    async def test_twenty_first_message_cannot_leak_its_attachment(self) -> None:
        self.assertEqual(MAX_BATCH_SIZE, 20)
        service = self.make_service()
        last_receipt = None
        for message_id in range(1, MAX_BATCH_SIZE + 1):
            last_receipt = await service.collect_message(
                owner_id=10,
                chat_id=100,
                message_thread_id=None,
                message=message(message_id),
            )
        assert last_receipt is not None
        self.assertEqual(self.scheduler.delays[last_receipt.deadline_job_id], 0.0)

        with self.assertRaisesRegex(
            BatchLimitExceededError, "batch_message_limit"
        ):
            await service.collect_message(
                owner_id=10,
                chat_id=100,
                message_thread_id=None,
                message=message(21, media_kind=MediaKind.PHOTO),
                attachment=photo_attachment(21),
            )

        await self.scheduler.fire(last_receipt.deadline_job_id)
        durable = await self.repository.get_by_id(last_receipt.draft_id)
        assert durable is not None
        self.assertEqual(len(durable.source_messages), MAX_BATCH_SIZE)
        self.assertEqual(durable.attachments, ())

    async def test_prompt_budget_rejection_leaves_existing_batch_unchanged(self) -> None:
        service = self.make_service(max_prompt_characters=5)
        first = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=None,
            message=message(30, text="1234"),
        )
        with self.assertRaisesRegex(
            PromptBudgetExceededError, "prompt_character_budget"
        ):
            await service.collect_message(
                owner_id=10,
                chat_id=100,
                message_thread_id=None,
                message=message(31, text="56"),
            )

        await self.scheduler.fire(first.deadline_job_id)
        durable = await self.repository.get_by_id(first.draft_id)
        assert durable is not None
        self.assertEqual([item.message_id for item in durable.source_messages], [30])

    async def test_attachment_eligibility_is_checked_before_collection(self) -> None:
        service = self.make_service(max_attachment_bytes=500)
        invalid_cases = (
            (message(40, media_kind=MediaKind.PHOTO), None),
            (message(41), photo_attachment(41)),
            (
                message(42, media_kind=MediaKind.PHOTO),
                Attachment(
                    file_id="document",
                    file_unique_id="document-unique",
                    media_kind=MediaKind.DOCUMENT,
                ),
            ),
            (
                message(43, media_kind=MediaKind.PHOTO),
                photo_attachment(43, file_size=501),
            ),
        )
        for source, attachment in invalid_cases:
            with self.subTest(message_id=source.message_id):
                with self.assertRaises(AttachmentEligibilityError):
                    await service.collect_message(
                        owner_id=10,
                        chat_id=100,
                        message_thread_id=None,
                        message=source,
                        attachment=attachment,
                    )

        self.assertEqual(
            await service.pending_count(
                owner_id=10, chat_id=100, message_thread_id=None
            ),
            0,
        )
        self.assertEqual(self.scheduler.active, set())

    async def test_stale_cancelled_deadline_cannot_flush_extended_batch(self) -> None:
        service = self.make_service()
        first = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=None,
            message=message(50),
        )
        second = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=None,
            message=message(51),
        )
        self.assertIn(first.deadline_job_id, self.scheduler.cancelled)

        stale_result = await self.scheduler.fire(
            first.deadline_job_id, include_cancelled=True
        )
        self.assertIsNone(stale_result)
        self.assertIsNone(await self.repository.get_by_id(first.draft_id))

        await self.scheduler.fire(second.deadline_job_id)
        durable = await self.repository.get_by_id(second.draft_id)
        assert durable is not None
        self.assertEqual(
            [item.message_id for item in durable.source_messages], [50, 51]
        )

    async def test_analysis_failure_is_durable_and_uses_safe_error_code(self) -> None:
        failing_analyzer = RecordingAnalyzer(fail=True)
        service = self.make_service(analyzer=failing_analyzer)
        receipt = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=None,
            message=message(60),
        )

        failed = await self.scheduler.fire(receipt.deadline_job_id)

        self.assertEqual(failed.state, DraftState.ANALYSIS_FAILED)  # type: ignore[union-attr]
        self.assertEqual(failed.last_error, "analysis_failed")  # type: ignore[union-attr]
        self.assertIsNone(failed.template)  # type: ignore[union-attr]
        self.assertEqual(len(self.failed_drafts), 1)
        self.assertEqual(self.ready_drafts, [])

    async def test_pending_deadlines_are_explicitly_cancellable(self) -> None:
        service = self.make_service()
        first = await service.collect_message(
            owner_id=10,
            chat_id=100,
            message_thread_id=None,
            message=message(70),
        )
        second = await service.collect_message(
            owner_id=10,
            chat_id=200,
            message_thread_id=None,
            message=message(71, chat_id=200),
        )

        self.assertTrue(
            await service.cancel_pending(
                owner_id=10, chat_id=100, message_thread_id=None
            )
        )
        self.assertIn(first.deadline_job_id, self.scheduler.cancelled)
        self.assertEqual(await service.cancel_all_pending(), 1)
        self.assertIn(second.deadline_job_id, self.scheduler.cancelled)
        self.assertEqual(self.scheduler.active, set())


if __name__ == "__main__":
    unittest.main()
