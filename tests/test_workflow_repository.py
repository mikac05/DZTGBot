"""Crash-safety and concurrency tests for the SQLite workflow repository."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dztgbot.domain.callbacks import (
    CallbackAction,
    build_token_record,
    generate_opaque_token,
)
from dztgbot.domain.errors import RevisionConflictError
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import (
    Attachment,
    Draft,
    JiraTaskTemplate,
    MediaKind,
    PublishedIssue,
    SourceMessageRef,
    SubmissionAttempt,
)
from dztgbot.infrastructure.persistence.workflow_sqlite import (
    AttachmentStatus,
    SQLiteWorkflowRepository,
    WorkflowDataError,
)


NOW = datetime(2026, 8, 8, 4, 0, tzinfo=timezone.utc)


def make_draft(
    draft_id: str,
    *,
    state: DraftState = DraftState.REVIEW,
    revision: int = 3,
    updated_at: datetime = NOW,
    with_content: bool = False,
) -> Draft:
    template = None
    source_messages: tuple[SourceMessageRef, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    published_issue = None
    if with_content:
        template = JiraTaskTemplate(
            project_key="BOT",
            issue_type="Task",
            summary="Durable workflow",
            description="Persist every field across restart.",
            priority="High",
            labels=("telegram", "durable"),
            components=("bot",),
            assignee="owner",
            acceptance_criteria=["State survives restart"],
        )
        source_messages = (
            SourceMessageRef(
                message_id=41,
                chat_id=-500,
                sender_id=101,
                text="Create a durable Jira issue",
                media_kind=MediaKind.TEXT,
                received_at=NOW - timedelta(minutes=3),
            ),
        )
        attachments = (
            Attachment(
                file_id="telegram-file-id",
                file_unique_id="telegram-unique-id",
                media_kind=MediaKind.PHOTO,
                file_name="evidence.jpg",
                file_size=4096,
                uploaded_attachment_id="jira-attachment-9",
            ),
        )
        published_issue = PublishedIssue(
            issue_key="BOT-9",
            issue_id="10009",
            issue_url="https://jira.invalid/browse/BOT-9",
            published_at=NOW - timedelta(minutes=1),
        )
    return Draft(
        draft_id=draft_id,
        owner_id=101,
        chat_id=-500,
        message_thread_id=17,
        state=state,
        revision=revision,
        template=template,
        source_messages=source_messages,
        attachments=attachments,
        created_at=NOW - timedelta(minutes=10),
        updated_at=updated_at,
        published_issue=published_issue,
        last_error="provider_timeout" if state is DraftState.SUBMISSION_UNKNOWN else None,
    )


class SQLiteWorkflowRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "workflows.sqlite3"
        self.repository = SQLiteWorkflowRepository(
            self.database_path, busy_timeout_seconds=0.25
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def asyncSetUp(self) -> None:
        await self.repository.initialize()

    async def test_complete_aggregate_and_callback_survive_restart(self) -> None:
        draft = make_draft("draft-roundtrip", with_content=True)
        expiry = NOW + timedelta(hours=1)
        await self.repository.save(draft, expires_at=expiry)

        opaque_token = generate_opaque_token()
        callback = build_token_record(
            opaque_token=opaque_token,
            draft_id=draft.draft_id,
            owner_user_id=draft.owner_id,
            chat_id=draft.chat_id,
            message_thread_id=draft.message_thread_id,
            preview_message_id=800,
            action=CallbackAction.CONFIRM,
            expected_revision=draft.revision,
            expected_state=draft.state.value,
            expires_at=expiry,
        )
        await self.repository.store_callback(callback)

        restarted = SQLiteWorkflowRepository(
            self.database_path, busy_timeout_seconds=0.25
        )
        await restarted.initialize()

        self.assertEqual(await restarted.get_by_id(draft.draft_id), draft)
        self.assertEqual(await restarted.get_expiry(draft.draft_id), expiry)
        self.assertEqual(await restarted.get_callback(callback.token_hash), callback)
        self.assertEqual(
            await restarted.get_published_issue(draft.draft_id),
            draft.published_issue,
        )
        stored_attachments = await restarted.list_attachments(draft.draft_id)
        self.assertEqual(len(stored_attachments), 1)
        self.assertEqual(stored_attachments[0].status, AttachmentStatus.UPLOADED)
        self.assertEqual(stored_attachments[0].attachment, draft.attachments[0])

        connection = sqlite3.connect(self.database_path)
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(callback_tokens)")
            }
        finally:
            connection.close()
        self.assertIn("token_hash", columns)
        self.assertNotIn("opaque_token", columns)
        self.assertNotIn("raw_token", columns)
        self.assertNotIn(opaque_token.encode("ascii"), self.database_path.read_bytes())

    async def test_atomic_compare_and_swap_has_one_winner(self) -> None:
        draft = make_draft("draft-cas")
        await self.repository.save(draft)

        outcomes = await asyncio.gather(
            self.repository.compare_and_swap_state(
                draft.draft_id, draft.revision, DraftState.SUBMITTING
            ),
            self.repository.compare_and_swap_state(
                draft.draft_id, draft.revision, DraftState.SUBMITTING
            ),
            return_exceptions=True,
        )

        winners = [outcome for outcome in outcomes if isinstance(outcome, Draft)]
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, RevisionConflictError)
        ]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(winners[0].state, DraftState.SUBMITTING)
        self.assertEqual(winners[0].revision, draft.revision + 1)

        restarted = SQLiteWorkflowRepository(self.database_path)
        await restarted.initialize()
        durable = await restarted.get_by_id(draft.draft_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable.state, DraftState.SUBMITTING)
        self.assertEqual(durable.revision, draft.revision + 1)

    async def test_submission_attempt_claim_has_one_winner_and_recovers(self) -> None:
        draft = make_draft(
            "draft-attempt", state=DraftState.SUBMITTING, revision=4
        )
        await self.repository.save(draft)
        attempts = (
            SubmissionAttempt(
                attempt_id="attempt-a",
                draft_id=draft.draft_id,
                request_hash="hash-a",
                attempt_number=1,
                started_at=NOW,
            ),
            SubmissionAttempt(
                attempt_id="attempt-b",
                draft_id=draft.draft_id,
                request_hash="hash-b",
                attempt_number=2,
                started_at=NOW,
            ),
        )

        claimed = await asyncio.gather(
            *(self.repository.claim_attempt(attempt) for attempt in attempts)
        )
        self.assertEqual(claimed.count(True), 1)
        self.assertEqual(claimed.count(False), 1)

        restarted = SQLiteWorkflowRepository(self.database_path)
        await restarted.initialize()
        durable_attempt = await restarted.get_latest_attempt(draft.draft_id)
        self.assertIsNotNone(durable_attempt)
        assert durable_attempt is not None
        self.assertEqual(durable_attempt.status, "pending")
        self.assertIn(durable_attempt.attempt_id, {"attempt-a", "attempt-b"})

        unknown = replace(
            durable_attempt,
            status="unknown",
            completed_at=NOW + timedelta(seconds=10),
            error_summary="outcome_unknown",
        )
        await restarted.update_attempt(unknown)
        self.assertEqual(
            (await restarted.get_latest_attempt(draft.draft_id)).status,  # type: ignore[union-attr]
            "unknown",
        )

        with self.assertRaisesRegex(WorkflowDataError, "attempt_identity_mismatch"):
            await restarted.update_attempt(
                replace(unknown, request_hash="different-request")
            )

    async def test_callback_consumption_has_one_winner(self) -> None:
        draft = make_draft("draft-callback")
        await self.repository.save(draft)
        callback = build_token_record(
            opaque_token=generate_opaque_token(),
            draft_id=draft.draft_id,
            owner_user_id=draft.owner_id,
            chat_id=draft.chat_id,
            message_thread_id=draft.message_thread_id,
            preview_message_id=801,
            action=CallbackAction.CANCEL,
            expected_revision=draft.revision,
            expected_state=draft.state.value,
            expires_at=NOW + timedelta(hours=1),
        )
        await self.repository.store_callback(callback)

        outcomes = await asyncio.gather(
            self.repository.consume_callback(callback.token_hash, NOW),
            self.repository.consume_callback(callback.token_hash, NOW),
        )
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 1)

    async def test_non_one_shot_callback_is_not_consumed(self) -> None:
        draft = make_draft("draft-toggle")
        await self.repository.save(draft)
        callback = build_token_record(
            opaque_token=generate_opaque_token(),
            draft_id=draft.draft_id,
            owner_user_id=draft.owner_id,
            chat_id=draft.chat_id,
            message_thread_id=draft.message_thread_id,
            preview_message_id=802,
            action=CallbackAction.TOGGLE_TYPE,
            expected_revision=draft.revision,
            expected_state=draft.state.value,
            expires_at=NOW + timedelta(hours=1),
        )
        self.assertFalse(callback.one_shot)
        await self.repository.store_callback(callback)

        self.assertFalse(
            await self.repository.consume_callback(callback.token_hash, NOW)
        )
        self.assertIsNone(
            (await self.repository.get_callback(callback.token_hash)).consumed_at  # type: ignore[union-attr]
        )

    async def test_ordinary_expiry_and_retention_never_delete_unknown(self) -> None:
        old_time = NOW - timedelta(days=30)
        draft = make_draft(
            "draft-unknown",
            state=DraftState.SUBMISSION_UNKNOWN,
            revision=7,
            updated_at=old_time,
        )
        await self.repository.save(
            draft, expires_at=NOW - timedelta(days=29)
        )

        self.assertEqual(await self.repository.list_expired(NOW), ())
        self.assertEqual(await self.repository.expire_eligible(NOW), 0)
        self.assertFalse(await self.repository.delete(draft.draft_id))
        self.assertEqual(await self.repository.delete_terminal(NOW), 0)

        restarted = SQLiteWorkflowRepository(self.database_path)
        await restarted.initialize()
        durable = await restarted.get_by_id(draft.draft_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable.state, DraftState.SUBMISSION_UNKNOWN)
        self.assertEqual(durable.revision, draft.revision)
        self.assertEqual(
            await restarted.get_expiry(draft.draft_id),
            NOW - timedelta(days=29),
        )

    async def test_expiry_transitions_only_eligible_state(self) -> None:
        draft = make_draft("draft-expiring", state=DraftState.REVIEW)
        await self.repository.save(draft, expires_at=NOW - timedelta(seconds=1))

        expired = await self.repository.list_expired(NOW)
        self.assertEqual([item.draft_id for item in expired], [draft.draft_id])
        self.assertEqual(await self.repository.expire_eligible(NOW), 1)

        durable = await self.repository.get_by_id(draft.draft_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable.state, DraftState.EXPIRED)
        self.assertEqual(durable.revision, draft.revision + 1)
        self.assertIsNone(await self.repository.get_expiry(draft.draft_id))

    async def test_attachment_transfer_status_uses_compare_and_swap(self) -> None:
        attachment = Attachment(
            file_id="file-id",
            file_unique_id="unique-id",
            media_kind=MediaKind.DOCUMENT,
            file_name="trace.txt",
            file_size=100,
        )
        draft = replace(
            make_draft("draft-attachment", state=DraftState.ATTACHING),
            attachments=(attachment,),
        )
        await self.repository.save(draft)

        uploading = await asyncio.gather(
            self.repository.set_attachment_status(
                draft.draft_id,
                attachment.file_unique_id,
                expected_status=AttachmentStatus.PENDING,
                target_status=AttachmentStatus.UPLOADING,
                updated_at=NOW,
            ),
            self.repository.set_attachment_status(
                draft.draft_id,
                attachment.file_unique_id,
                expected_status=AttachmentStatus.PENDING,
                target_status=AttachmentStatus.UPLOADING,
                updated_at=NOW,
            ),
        )
        self.assertEqual(uploading.count(True), 1)
        self.assertEqual(uploading.count(False), 1)
        self.assertTrue(
            await self.repository.set_attachment_status(
                draft.draft_id,
                attachment.file_unique_id,
                expected_status=AttachmentStatus.UPLOADING,
                target_status=AttachmentStatus.UPLOADED,
                updated_at=NOW + timedelta(seconds=1),
                uploaded_attachment_id="jira-attachment-10",
            )
        )

        restarted = SQLiteWorkflowRepository(self.database_path)
        await restarted.initialize()
        durable = await restarted.list_attachments(draft.draft_id)
        self.assertEqual(durable[0].status, AttachmentStatus.UPLOADED)
        self.assertEqual(
            durable[0].attachment.uploaded_attachment_id, "jira-attachment-10"
        )

    async def test_aggregate_resave_preserves_attachment_recovery_state(self) -> None:
        attachment = Attachment(
            file_id="failed-file-id",
            file_unique_id="failed-unique-id",
            media_kind=MediaKind.DOCUMENT,
        )
        draft = replace(
            make_draft("draft-failed-attachment", state=DraftState.ATTACHING),
            attachments=(attachment,),
        )
        await self.repository.save(draft)
        self.assertTrue(
            await self.repository.set_attachment_status(
                draft.draft_id,
                attachment.file_unique_id,
                expected_status=AttachmentStatus.PENDING,
                target_status=AttachmentStatus.UPLOADING,
                updated_at=NOW,
            )
        )
        self.assertTrue(
            await self.repository.set_attachment_status(
                draft.draft_id,
                attachment.file_unique_id,
                expected_status=AttachmentStatus.UPLOADING,
                target_status=AttachmentStatus.FAILED,
                updated_at=NOW + timedelta(seconds=1),
                last_error_code="upload_timeout",
            )
        )

        loaded = await self.repository.get_by_id(draft.draft_id)
        assert loaded is not None
        await self.repository.save(loaded)

        restarted = SQLiteWorkflowRepository(self.database_path)
        await restarted.initialize()
        durable = await restarted.list_attachments(draft.draft_id)
        self.assertEqual(durable[0].status, AttachmentStatus.FAILED)
        self.assertEqual(durable[0].last_error_code, "upload_timeout")


if __name__ == "__main__":
    unittest.main()
