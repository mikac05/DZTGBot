"""SQLite restart matrix for every durable Phase 6 workflow state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import (
    Attachment,
    Draft,
    JiraTaskTemplate,
    PublishedIssue,
    SourceMessageRef,
)
from dztgbot.domain.policy import DenialCode
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.services.callback_service import CallbackService


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return NOW


def durable_draft(state: DraftState, ordinal: int) -> Draft:
    issue = PublishedIssue(
        f"BOT-{ordinal}",
        str(ordinal),
        f"https://jira.invalid/browse/BOT-{ordinal}",
        NOW,
    )
    return Draft(
        draft_id=f"state-{state.value}",
        owner_id=ordinal + 1,
        chat_id=ordinal + 1,
        message_thread_id=ordinal + 100,
        state=state,
        revision=ordinal + 1,
        template=JiraTaskTemplate(
            "BOT",
            "Task",
            f"summary-{state.value}",
            "description",
            "Medium",
            labels=("phase6",),
            acceptance_criteria=["survives restart"],
        ),
        source_messages=(
            SourceMessageRef(
                message_id=ordinal + 1,
                chat_id=ordinal + 1,
                sender_id=ordinal + 1000,
                text="durable source",
                received_at=NOW,
            ),
        ),
        attachments=(
            Attachment(
                f"file-{ordinal}",
                f"unique-{ordinal}",
                file_name=f"photo-{ordinal}.jpg",
                file_size=10,
            ),
        ),
        created_at=NOW,
        updated_at=NOW,
        published_issue=issue,
        last_error="safe_code" if "failed" in state.value or "unknown" in state.value else None,
    )


class RestartMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "restart.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.path, enable_wal=False)
        await self.repository.initialize()

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.temporary_directory.cleanup()

    async def test_every_fsm_state_round_trips_across_repository_restart(self) -> None:
        expected: dict[str, Draft] = {}
        for ordinal, state in enumerate(DraftState, start=1):
            item = durable_draft(state, ordinal)
            expected[item.draft_id] = item
            await self.repository.save(item)

        await self.repository.close()
        reopened = SQLiteWorkflowRepository(self.path, enable_wal=False)
        await reopened.initialize()
        try:
            self.assertEqual(len(expected), len(DraftState))
            for draft_id, item in expected.items():
                with self.subTest(state=item.state.value):
                    restored = await reopened.get_by_id(draft_id)
                    self.assertEqual(restored, item)
        finally:
            await reopened.close()

    async def test_consumed_callback_remains_consumed_after_restart(self) -> None:
        draft = durable_draft(DraftState.REVIEW, 50)
        await self.repository.save(draft)
        callbacks = CallbackService(self.repository, self.repository, FixedClock())
        issued = await callbacks.issue_preview_buttons(
            draft,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=900,
        )
        raw = issued[CallbackAction.CONFIRM].callback_data
        first = await callbacks.authorize(
            raw_callback_data=raw,
            actor_user_id=draft.owner_id,
            chat_id=draft.chat_id,
            chat_type="private",
            message_thread_id=draft.message_thread_id,
            preview_message_id=900,
        )
        self.assertTrue(first.allowed)

        await self.repository.close()
        reopened = SQLiteWorkflowRepository(self.path, enable_wal=False)
        await reopened.initialize()
        try:
            replay = await CallbackService(
                reopened, reopened, FixedClock()
            ).authorize(
                raw_callback_data=raw,
                actor_user_id=draft.owner_id,
                chat_id=draft.chat_id,
                chat_type="private",
                message_thread_id=draft.message_thread_id,
                preview_message_id=900,
            )
            self.assertFalse(replay.allowed)
            self.assertEqual(replay.denial_code, DenialCode.TOKEN_CONSUMED)
        finally:
            await reopened.close()


if __name__ == "__main__":
    unittest.main()
