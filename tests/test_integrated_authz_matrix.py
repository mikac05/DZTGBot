"""Phase 6 Task P6-G — integrated authorization matrix.

Verifies actor / chat / thread / preview-message / action / state / revision /
expiry binding, callback replay, token-possession-alone denial, and one-shot
concurrency through CallbackService + SQLiteWorkflowRepository + composed
callback handlers. No live network.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from dztgbot.domain.callbacks import (
    CallbackAction,
    encode_callback_data,
    generate_opaque_token,
    hash_opaque_token,
)
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate, PublishedIssue
from dztgbot.domain.policy import DenialCode, user_message_for_denial
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.services.callback_service import CallbackService
from dztgbot.services.submission_service import SubmissionService
from dztgbot.services.workflow_service import WorkflowService
from dztgbot.ui.handlers.callbacks import handle_callback_query


NOW = datetime(2026, 8, 8, 17, 0, tzinfo=timezone.utc)
OWNER_ID = 42
CHAT_ID = 42
PREVIEW_ID = 700


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


class SequenceIds:
    def __init__(self) -> None:
        self._n = 0

    def generate_uuid(self) -> str:
        self._n += 1
        return f"authz-{self._n}"


class SuccessGateway:
    def __init__(self) -> None:
        self.create_calls = 0

    async def create_issue(self, template, pat, idempotency_key=None):  # type: ignore[no-untyped-def]
        self.create_calls += 1
        return PublishedIssue(
            issue_key=f"BOT-{100 + self.create_calls}",
            issue_id=str(100 + self.create_calls),
            issue_url=f"https://jira.invalid/browse/BOT-{100 + self.create_calls}",
            published_at=NOW,
        )

    async def update_issue(self, issue_key, template, pat):  # type: ignore[no-untyped-def]
        return None

    async def find_by_request_hash(self, project_key, request_hash, pat):  # type: ignore[no-untyped-def]
        return ()

    async def get_issue(self, issue_key, pat):  # type: ignore[no-untyped-def]
        return SimpleNamespace(fields={})


def _template() -> JiraTaskTemplate:
    return JiraTaskTemplate("BOT", "Task", "authz summary", "authz description", "Medium")


def _callback_update(
    *,
    callback_data: str,
    user_id: int = OWNER_ID,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
    message_id: int = PREVIEW_ID,
    message_thread_id: int | None = None,
) -> MagicMock:
    user = SimpleNamespace(id=user_id, full_name="Owner", username="owner")
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=None)
    message = MagicMock()
    message.message_id = message_id
    message.message_thread_id = message_thread_id
    message.text = None
    query = AsyncMock()
    query.data = callback_data
    query.message = message
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    update.callback_query = query
    return update


class IntegratedAuthzMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(
            Path(self._tmp.name) / "authz.sqlite3",
            enable_wal=False,
        )
        await self.repository.initialize()
        self.clock = FixedClock()
        self.workflow = WorkflowService(
            repository=self.repository,
            clock=self.clock,
            id_generator=SequenceIds(),
        )
        self.callbacks = CallbackService(
            drafts=self.repository,
            tokens=self.repository,
            clock=self.clock,
        )
        self.gateway = SuccessGateway()
        self.submission = SubmissionService(self.repository, self.gateway)
        self.user_store = MagicMock()
        self.user_store.get_credentials = MagicMock(
            return_value=SimpleNamespace(pat="TEST_ONLY_AUTHZ_PAT")
        )

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self._tmp.cleanup()

    async def _create_review_draft(
        self,
        *,
        owner_id: int = OWNER_ID,
        chat_id: int = CHAT_ID,
        thread_id: int | None = None,
        preview_message_id: int = PREVIEW_ID,
        actions: tuple[CallbackAction, ...] = (
            CallbackAction.CONFIRM,
            CallbackAction.CANCEL,
            CallbackAction.TOGGLE_TYPE,
        ),
    ) -> tuple[Draft, dict[CallbackAction, str]]:
        draft = await self.workflow.create_manual_draft(
            owner_id=owner_id,
            chat_id=chat_id,
            template=_template(),
            message_thread_id=thread_id,
        )
        issued = await self.callbacks.issue_preview_buttons(
            draft,
            actions=actions,
            preview_message_id=preview_message_id,
            message_thread_id=thread_id,
        )
        return draft, {action: button.callback_data for action, button in issued.items()}

    async def _authorize(
        self,
        raw: str,
        *,
        actor_user_id: int = OWNER_ID,
        chat_id: int = CHAT_ID,
        chat_type: str = "private",
        message_thread_id: int | None = None,
        preview_message_id: int = PREVIEW_ID,
    ):
        return await self.callbacks.authorize(
            raw_callback_data=raw,
            actor_user_id=actor_user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            message_thread_id=message_thread_id,
            preview_message_id=preview_message_id,
        )

    async def test_happy_path_bound_confirm_allowed_once(self) -> None:
        draft, buttons = await self._create_review_draft()
        result = await self._authorize(buttons[CallbackAction.CONFIRM])
        self.assertTrue(result.allowed)
        self.assertEqual(result.action, CallbackAction.CONFIRM)
        assert result.draft is not None
        self.assertEqual(result.draft.draft_id, draft.draft_id)

        replay = await self._authorize(buttons[CallbackAction.CONFIRM])
        self.assertFalse(replay.allowed)
        self.assertEqual(replay.denial_code, DenialCode.TOKEN_CONSUMED)

    async def test_matrix_denies_wrong_actor_chat_thread_message_action_and_state(
        self,
    ) -> None:
        draft, buttons = await self._create_review_draft(thread_id=9)
        confirm = buttons[CallbackAction.CONFIRM]
        cancel = buttons[CallbackAction.CANCEL]

        matrix = (
            ("foreign_actor", dict(actor_user_id=99, message_thread_id=9), DenialCode.FOREIGN_ACTOR),
            ("wrong_chat", dict(chat_id=999, message_thread_id=9), DenialCode.WRONG_CHAT),
            ("wrong_thread", dict(message_thread_id=8), DenialCode.WRONG_THREAD),
            (
                "wrong_preview_message",
                dict(preview_message_id=1, message_thread_id=9),
                DenialCode.WRONG_MESSAGE,
            ),
            ("group_chat", dict(chat_type="group", message_thread_id=9), DenialCode.NOT_PRIVATE_CHAT),
        )
        for name, kwargs, expected in matrix:
            with self.subTest(name=name):
                result = await self._authorize(confirm, **kwargs)
                self.assertEqual(result.denial_code, expected)
                message = result.user_message or ""
                self.assertEqual(message, user_message_for_denial(expected))
                self.assertNotIn("j1:", message)
                self.assertNotIn(confirm, message)

        # Confirm opaque token presented under a different wire action → action mismatch.
        mismatched = await self._authorize(
            encode_callback_data(
                CallbackAction.CANCEL,
                buttons[CallbackAction.CONFIRM].rsplit(":", 1)[-1],
            ),
            message_thread_id=9,
        )
        self.assertEqual(mismatched.denial_code, DenialCode.ACTION_MISMATCH)
        self.assertNotIn(confirm, mismatched.user_message or "")

        # Cancel consumes/allows once; subsequent confirm against cancelled draft fails closed.
        cancel_result = await self._authorize(cancel, message_thread_id=9)
        self.assertTrue(cancel_result.allowed)
        await self.repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.CANCELLED
        )
        stale_after_cancel = await self._authorize(confirm, message_thread_id=9)
        self.assertIn(
            stale_after_cancel.denial_code,
            {
                DenialCode.TOKEN_CONSUMED,
                DenialCode.ILLEGAL_STATE,
                DenialCode.STALE_REVISION,
                DenialCode.TOKEN_EXPIRED,
            },
        )

    async def test_stale_revision_and_expiry_denied(self) -> None:
        draft, buttons = await self._create_review_draft(
            actions=(CallbackAction.CONFIRM, CallbackAction.TOGGLE_TYPE)
        )
        confirm = buttons[CallbackAction.CONFIRM]
        toggle = buttons[CallbackAction.TOGGLE_TYPE]

        # SQLite authority bumps revision via CAS (plain save cannot rewrite revision).
        await self.repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.EDITING
        )
        stale = await self._authorize(confirm)
        self.assertEqual(stale.denial_code, DenialCode.STALE_REVISION)

        # Return to REVIEW for a fresh token issue, then expire by clock.
        editing = await self.repository.get_by_id(draft.draft_id)
        assert editing is not None
        reviewing = await self.repository.compare_and_swap_state(
            editing.draft_id, editing.revision, DraftState.REVIEW
        )
        issued = await self.callbacks.issue_preview_buttons(
            reviewing,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=PREVIEW_ID,
        )
        self.clock.advance(timedelta(hours=2))
        expired = await self._authorize(issued[CallbackAction.CONFIRM].callback_data)
        self.assertEqual(expired.denial_code, DenialCode.TOKEN_EXPIRED)

        # Prior toggle token is invalid after revision/state movement and reissue.
        old_toggle = await self._authorize(toggle)
        self.assertIn(
            old_toggle.denial_code,
            {
                DenialCode.TOKEN_EXPIRED,
                DenialCode.STALE_REVISION,
                DenialCode.UNKNOWN_TOKEN,
                DenialCode.ILLEGAL_STATE,
            },
        )

    async def test_token_possession_alone_is_insufficient(self) -> None:
        """Database/token possession without matching actor/chat/message is denied."""

        draft, buttons = await self._create_review_draft()
        raw = buttons[CallbackAction.CONFIRM]
        token_hash = hash_opaque_token(raw.rsplit(":", 1)[-1])
        stored = await self.repository.get_callback(token_hash)
        self.assertIsNotNone(stored)

        # Attacker has the wire callback_data (token possession) but wrong actor.
        foreign = await self._authorize(raw, actor_user_id=999)
        self.assertEqual(foreign.denial_code, DenialCode.FOREIGN_ACTOR)

        wrong_chat = await self._authorize(raw, chat_id=999)
        self.assertEqual(wrong_chat.denial_code, DenialCode.WRONG_CHAT)

        wrong_msg = await self._authorize(raw, preview_message_id=1)
        self.assertEqual(wrong_msg.denial_code, DenialCode.WRONG_MESSAGE)

        unknown = await self._authorize(
            encode_callback_data(CallbackAction.CONFIRM, generate_opaque_token())
        )
        self.assertEqual(unknown.denial_code, DenialCode.UNKNOWN_TOKEN)

        # Draft still REVIEW — no mutation occurred from possession-only probes.
        current = await self.repository.get_by_id(draft.draft_id)
        assert current is not None
        self.assertEqual(current.state, DraftState.REVIEW)
        self.assertEqual(current.revision, 1)

    async def test_one_shot_concurrency_single_winner_through_sqlite(self) -> None:
        draft, buttons = await self._create_review_draft(
            actions=(CallbackAction.CONFIRM,)
        )
        raw = buttons[CallbackAction.CONFIRM]

        results = await asyncio.gather(
            self._authorize(raw),
            self._authorize(raw),
            self._authorize(raw),
        )
        winners = [r for r in results if r.allowed]
        losers = [r for r in results if not r.allowed]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 2)
        for loser in losers:
            self.assertEqual(loser.denial_code, DenialCode.TOKEN_CONSUMED)

        # Token row is consumed in SQLite.
        token_hash = hash_opaque_token(raw.rsplit(":", 1)[-1])
        record = await self.repository.get_callback(token_hash)
        assert record is not None
        self.assertIsNotNone(record.consumed_at)

    async def test_handler_layer_enforces_same_matrix_and_denies_replay(self) -> None:
        draft, buttons = await self._create_review_draft(
            actions=(CallbackAction.CONFIRM,)
        )
        raw = buttons[CallbackAction.CONFIRM]

        # Foreign actor through composed handler.
        foreign_update = _callback_update(callback_data=raw, user_id=999)
        await handle_callback_query(
            foreign_update,
            MagicMock(user_data={}),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
            submission_service=self.submission,
            user_store=self.user_store,
        )
        foreign_update.callback_query.answer.assert_awaited()
        foreign_text = foreign_update.callback_query.answer.await_args.args[0]
        self.assertEqual(
            foreign_text, user_message_for_denial(DenialCode.FOREIGN_ACTOR)
        )
        self.assertNotIn(raw, foreign_text)

        # Legitimate first click succeeds.
        ok_update = _callback_update(callback_data=raw)
        await handle_callback_query(
            ok_update,
            MagicMock(user_data={}),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
            submission_service=self.submission,
            user_store=self.user_store,
        )
        final = await self.repository.get_by_id(draft.draft_id)
        assert final is not None
        self.assertEqual(final.state, DraftState.CREATED)
        self.assertEqual(self.gateway.create_calls, 1)

        # Replay through handler is denied and does not re-create.
        replay_update = _callback_update(callback_data=raw)
        await handle_callback_query(
            replay_update,
            MagicMock(user_data={}),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
            submission_service=self.submission,
            user_store=self.user_store,
        )
        replay_text = replay_update.callback_query.answer.await_args.args[0]
        self.assertEqual(
            replay_text, user_message_for_denial(DenialCode.TOKEN_CONSUMED)
        )
        self.assertEqual(self.gateway.create_calls, 1)
        final2 = await self.repository.get_by_id(draft.draft_id)
        assert final2 is not None
        self.assertEqual(final2.state, DraftState.CREATED)
        self.assertEqual(final2.published_issue.issue_key, "BOT-101")  # type: ignore[union-attr]

    async def test_toggle_is_not_one_shot_but_revision_binding_still_applies(
        self,
    ) -> None:
        draft, buttons = await self._create_review_draft(
            actions=(CallbackAction.TOGGLE_TYPE, CallbackAction.CONFIRM)
        )
        toggle = buttons[CallbackAction.TOGGLE_TYPE]

        first = await self._authorize(toggle)
        self.assertTrue(first.allowed)
        # Toggle authorize does not consume; second authorize still allowed until revision changes.
        second = await self._authorize(toggle)
        self.assertTrue(second.allowed)

        await self.repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.EDITING
        )
        after = await self._authorize(toggle)
        self.assertEqual(after.denial_code, DenialCode.STALE_REVISION)

    async def test_composed_toggle_handler_must_persist_revision_on_sqlite(
        self,
    ) -> None:
        """Composition defect probe: WorkflowService toggle/save vs SQLite revision rules.

        Owning interfaces if this fails:
        - ``src/dztgbot/services/workflow_service.py`` (``update_template`` uses ``save``)
        - ``src/dztgbot/infrastructure/persistence/workflow_sqlite.py`` (``save`` rejects revision rewrite)
        - ``src/dztgbot/ui/handlers/callbacks.py`` (toggle dispatches to WorkflowService)
        """

        draft, buttons = await self._create_review_draft(
            actions=(CallbackAction.TOGGLE_TYPE,)
        )
        raw = buttons[CallbackAction.TOGGLE_TYPE]
        update = _callback_update(callback_data=raw)
        await handle_callback_query(
            update,
            MagicMock(user_data={}),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
        )
        final = await self.repository.get_by_id(draft.draft_id)
        assert final is not None
        self.assertEqual(
            final.revision,
            2,
            msg=(
                "SOURCE BLOCKER: composed toggle cannot bump SQLite revision "
                "(workflow_service.update_template/save vs workflow_sqlite.save CAS rules)"
            ),
        )
        self.assertEqual(final.template.issue_type, "Bug")  # type: ignore[union-attr]

    async def test_already_processing_and_illegal_state_codes(self) -> None:
        draft, buttons = await self._create_review_draft(
            actions=(CallbackAction.CONFIRM,)
        )
        raw = buttons[CallbackAction.CONFIRM]

        # Force SUBMITTING via legal CAS while old REVIEW token still present.
        submitting = await self.repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.SUBMITTING
        )
        result = await self._authorize(raw)
        # Revision advanced by CAS → stale takes precedence over already-processing.
        self.assertIn(
            result.denial_code,
            {DenialCode.ALREADY_PROCESSING, DenialCode.STALE_REVISION},
        )

        # Fresh draft for illegal-state (cancelled) without revision bump relative to token.
        draft2, buttons2 = await self._create_review_draft(
            actions=(CallbackAction.CONFIRM,)
        )
        raw2 = buttons2[CallbackAction.CONFIRM]
        # Issue a second token set after moving to cancelled would expire old ones;
        # instead CAS cancel at same binding and accept stale or illegal.
        await self.repository.compare_and_swap_state(
            draft2.draft_id, draft2.revision, DraftState.CANCELLED
        )
        illegal = await self._authorize(raw2)
        self.assertIn(
            illegal.denial_code,
            {DenialCode.ILLEGAL_STATE, DenialCode.STALE_REVISION},
        )
        self.assertIsNotNone(submitting)


if __name__ == "__main__":
    unittest.main()
