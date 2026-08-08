"""Integrated durable recovery tests using local SQLite and deterministic providers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.errors import (
    ClassifiedOperationError,
    ErrorKind,
    Operation,
    classify_unknown_mutation_outcome,
)
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Attachment, Draft, JiraTaskTemplate, PublishedIssue
from dztgbot.infrastructure import AsyncTaskScheduler
from dztgbot.infrastructure.persistence.workflow_sqlite import (
    AttachmentStatus,
    SQLiteWorkflowRepository,
)
from dztgbot.services.attachment_service import AttachmentContent, AttachmentService
from dztgbot.services.submission_service import SubmissionService


NOW = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)


def template() -> JiraTaskTemplate:
    return JiraTaskTemplate("BOT", "Task", "summary", "description", "Medium")


def draft(
    draft_id: str,
    *,
    state: DraftState = DraftState.REVIEW,
    revision: int = 1,
    attachments: tuple[Attachment, ...] = (),
    published_issue: PublishedIssue | None = None,
) -> Draft:
    return Draft(
        draft_id=draft_id,
        owner_id=7,
        chat_id=7,
        state=state,
        revision=revision,
        template=template(),
        attachments=attachments,
        published_issue=published_issue,
        created_at=NOW,
        updated_at=NOW,
    )


class TrackingRepository(SQLiteWorkflowRepository):
    """Marks the exact duration of every SQLite operation awaited by a service."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, enable_wal=False)
        self.active_operations = 0

    async def _run(self, operation, *args):  # type: ignore[no-untyped-def]
        self.active_operations += 1
        try:
            return await super()._run(operation, *args)
        finally:
            self.active_operations -= 1


class CommitThenTimeoutGateway:
    def __init__(self, repository: TrackingRepository) -> None:
        self.repository = repository
        self.create_calls = 0
        self.created: dict[str, PublishedIssue] = {}

    async def create_issue(self, reviewed, pat, idempotency_key=None):  # type: ignore[no-untyped-def]
        self.assert_repository_released()
        self.create_calls += 1
        issue = PublishedIssue(
            "BOT-99", "99", "https://jira.invalid/browse/BOT-99", NOW
        )
        self.created[idempotency_key] = issue
        await asyncio.sleep(0)
        self.assert_repository_released()
        raise ClassifiedOperationError(
            classify_unknown_mutation_outcome(
                operation=Operation.JIRA_CREATE,
                kind=ErrorKind.TIMEOUT,
            )
        )

    async def find_by_request_hash(self, project_key, request_hash, pat):  # type: ignore[no-untyped-def]
        self.assert_repository_released()
        await asyncio.sleep(0)
        self.assert_repository_released()
        issue = self.created.get(request_hash)
        return () if issue is None else (issue,)

    def assert_repository_released(self) -> None:
        if self.repository.active_operations:
            raise AssertionError("provider await occurred during a repository operation")


class RestartableLoader:
    def __init__(self) -> None:
        self.fail_unique_ids = {"unique-2"}

    async def load(self, file_id: str) -> AttachmentContent:
        if file_id.endswith("2") and "unique-2" in self.fail_unique_ids:
            raise RuntimeError("provider payload")
        return AttachmentContent(b"image", f"{file_id}.jpg", "image/jpeg")


class AttachmentGateway:
    def __init__(self, repository: TrackingRepository) -> None:
        self.repository = repository
        self.uploaded: list[str] = []

    async def upload_attachment(
        self, issue_key, filename, content, mime_type, pat  # type: ignore[no-untyped-def]
    ) -> str:
        self.assert_repository_released()
        await asyncio.sleep(0)
        self.assert_repository_released()
        self.uploaded.append(filename)
        return f"attachment-{len(self.uploaded)}"

    def assert_repository_released(self) -> None:
        if self.repository.active_operations:
            raise AssertionError("attachment await occurred during a repository operation")


class IntegratedWorkflowRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "recovery.sqlite3"
        self.repository = TrackingRepository(self.database_path)
        await self.repository.initialize()

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.temporary_directory.cleanup()

    async def test_timeout_after_remote_commit_reconciles_without_duplicate_create(self) -> None:
        await self.repository.save(draft("unknown-create"))
        gateway = CommitThenTimeoutGateway(self.repository)
        service = SubmissionService(self.repository, gateway)

        unknown = await service.submit("unknown-create", "test-pat")
        self.assertEqual(unknown.draft.state, DraftState.SUBMISSION_UNKNOWN)
        self.assertTrue(unknown.reconciliation_pending)
        self.assertEqual(gateway.create_calls, 1)

        with self.assertRaises(Exception):
            await service.submit("unknown-create", "test-pat")
        self.assertEqual(gateway.create_calls, 1)

        reopened = TrackingRepository(self.database_path)
        await reopened.initialize()
        gateway.repository = reopened
        recovered = await SubmissionService(reopened, gateway).reconcile_create(
            "unknown-create", "test-pat"
        )
        self.assertEqual(recovered.draft.state, DraftState.CREATED)
        self.assertEqual(recovered.published_issue.issue_key, "BOT-99")  # type: ignore[union-attr]
        self.assertEqual(gateway.create_calls, 1)
        await reopened.close()

    async def test_partial_attachment_restart_retries_only_failed_transfer(self) -> None:
        issue = PublishedIssue(
            "BOT-10", "10", "https://jira.invalid/browse/BOT-10", NOW
        )
        attachments = (
            Attachment("file-1", "unique-1", file_name="one.jpg", file_size=5),
            Attachment("file-2", "unique-2", file_name="two.jpg", file_size=5),
        )
        await self.repository.save(
            draft(
                "partial",
                state=DraftState.CREATED,
                revision=2,
                attachments=attachments,
                published_issue=issue,
            )
        )
        loader = RestartableLoader()
        gateway = AttachmentGateway(self.repository)

        first = await AttachmentService(
            self.repository, gateway, loader
        ).upload_pending("partial", "test-pat")
        self.assertEqual(first.draft.state, DraftState.ATTACHMENT_PARTIAL)
        statuses = {
            record.attachment.file_unique_id: record.status
            for record in await self.repository.list_attachments("partial")
        }
        self.assertEqual(statuses["unique-1"], AttachmentStatus.UPLOADED)
        self.assertEqual(statuses["unique-2"], AttachmentStatus.FAILED)

        reopened = TrackingRepository(self.database_path)
        await reopened.initialize()
        gateway.repository = reopened
        loader.fail_unique_ids.clear()
        second = await AttachmentService(
            reopened, gateway, loader
        ).upload_pending("partial", "test-pat")
        self.assertEqual(second.draft.state, DraftState.COMPLETE)
        self.assertEqual(second.uploaded, 1)
        self.assertEqual(len(gateway.uploaded), 2)
        self.assertNotIn("create_issue", dir(gateway))
        await reopened.close()

    async def test_shutdown_cancels_owned_inflight_scheduler_task(self) -> None:
        scheduler = AsyncTaskScheduler()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def owned_job() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        scheduler.schedule_timer("owned", 0, owned_job)
        await started.wait()
        await scheduler.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(scheduler._tasks, {})
        self.assertEqual(scheduler._timers, {})


if __name__ == "__main__":
    unittest.main()
