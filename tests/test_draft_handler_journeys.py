"""Deterministic complete mocked journeys for Telegram UI handlers (Task P5-A).

Tests forward intake, manual draft creation, review preview, inline button toggles,
text reply edits, submission, cancellation, retry, reconciliation, attachments,
published issue edits, and security callback token invalidation / foreign user guards.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from dztgbot.domain.callbacks import CallbackAction, CallbackTokenRecord
from dztgbot.domain.errors import ClassifiedOperationError, RevisionConflictError
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import (
    Attachment,
    Draft,
    JiraTaskTemplate,
    PublishedIssue,
    SubmissionAttempt,
)
from dztgbot.services.attachment_service import AttachmentService
from dztgbot.services.callback_service import CallbackService
from dztgbot.services.submission_service import SubmissionService
from dztgbot.services.workflow_service import WorkflowService
from dztgbot.ui.handlers.callbacks import handle_callback_query
from dztgbot.ui.handlers.drafts import (
    handle_draft_reply_text,
    handle_forward_intake,
    handle_manual_create,
)


class InMemoryRepository:
    """In-memory repository implementing workflow, callback, and submission protocols."""

    def __init__(self) -> None:
        self.drafts: dict[str, Draft] = {}
        self.tokens: dict[str, CallbackTokenRecord] = {}
        self.attempts: dict[str, list[SubmissionAttempt]] = {}
        self.published_issues: dict[str, PublishedIssue] = {}
        self.attachments: dict[str, list] = {}

    async def save(self, draft: Draft, *, expires_at: datetime | None = None) -> None:
        self.drafts[draft.draft_id] = draft

    async def get_by_id(self, draft_id: str) -> Draft | None:
        return self.drafts.get(draft_id)

    async def compare_and_swap_state(
        self,
        draft_id: str,
        expected_revision: int,
        target_state: DraftState,
        last_error: str | None = None,
    ) -> Draft:
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise KeyError(f"Draft {draft_id} not found")
        if draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)

        updated = replace(
            draft,
            state=target_state,
            revision=draft.revision + 1,
            last_error=last_error,
            updated_at=datetime.now(timezone.utc),
        )
        self.drafts[draft_id] = updated
        return updated

    async def store_callback(self, record: CallbackTokenRecord) -> None:
        self.tokens[record.token_hash] = record

    async def get_callback(self, token_hash: str) -> CallbackTokenRecord | None:
        return self.tokens.get(token_hash)

    async def consume_callback(self, token_hash: str, consumed_at: datetime) -> bool:
        record = self.tokens.get(token_hash)
        if record is None or record.consumed_at is not None:
            return False
        updated = replace(record, consumed_at=consumed_at)
        self.tokens[token_hash] = updated
        return True

    async def invalidate_draft_preview_tokens(
        self, draft_id: str, *, at: datetime
    ) -> int:
        count = 0
        for th, rec in list(self.tokens.items()):
            if rec.draft_id == draft_id:
                del self.tokens[th]
                count += 1
        return count

    async def claim_attempt(self, attempt: SubmissionAttempt) -> bool:
        att_list = self.attempts.setdefault(attempt.draft_id, [])
        att_list.append(attempt)
        return True

    async def update_attempt(self, attempt: SubmissionAttempt) -> None:
        att_list = self.attempts.get(attempt.draft_id, [])
        for i, a in enumerate(att_list):
            if a.attempt_id == attempt.attempt_id:
                att_list[i] = attempt

    async def get_latest_attempt(self, draft_id: str) -> SubmissionAttempt | None:
        att_list = self.attempts.get(draft_id, [])
        return att_list[-1] if att_list else None

    async def store_published_issue(self, draft_id: str, issue: PublishedIssue) -> None:
        self.published_issues[draft_id] = issue
        draft = self.drafts.get(draft_id)
        if draft is not None:
            self.drafts[draft_id] = replace(draft, published_issue=issue)

    async def get_published_issue(self, draft_id: str) -> PublishedIssue | None:
        return self.published_issues.get(draft_id)

    async def list_attachments(self, draft_id: str) -> list:
        return self.attachments.get(draft_id, [])


class DummyGateway:
    """Mock SubmissionGateway for testing."""

    def __init__(self) -> None:
        self.should_fail_retryable = False
        self.should_timeout = False
        self.created_issues: list[PublishedIssue] = []

    async def create_issue(
        self, template: JiraTaskTemplate, pat: str, idempotency_key: str | None = None
    ) -> PublishedIssue:
        if self.should_fail_retryable:
            from dztgbot.domain.errors import classify_definite_mutation_failure, ErrorKind, Operation, SafeErrorCode
            raise ClassifiedOperationError(
                classify_definite_mutation_failure(
                    operation=Operation.JIRA_CREATE,
                    kind=ErrorKind.PROVIDER_REJECTION,
                    safe_code=SafeErrorCode.PROVIDER_REJECTED,
                )
            )
        if self.should_timeout:
            raise TimeoutError("Connection timed out")

        issue = PublishedIssue(
            issue_key=f"TW-{len(self.created_issues) + 100}",
            issue_id=f"10{len(self.created_issues) + 100}",
            issue_url=f"https://jira.example.com/browse/TW-{len(self.created_issues) + 100}",
        )
        self.created_issues.append(issue)
        return issue

    async def update_issue(
        self, issue_key: str, template: JiraTaskTemplate, pat: str
    ) -> None:
        pass

    async def find_by_request_hash(
        self, project_key: str, request_hash: str, pat: str
    ) -> list[PublishedIssue]:
        return self.created_issues

    async def get_issue(self, issue_key: str, pat: str) -> object:
        mock_issue = MagicMock()
        mock_issue.fields = {"project": {"key": project_key}}
        return mock_issue


class TestDraftHandlerJourneys(unittest.IsolatedAsyncioTestCase):
    """Async test suite for Telegram UI handler journeys and token binding."""

    async def asyncSetUp(self) -> None:
        self.repo = InMemoryRepository()
        self.workflow_service = WorkflowService(repository=self.repo)
        self.callback_service = CallbackService(drafts=self.repo, tokens=self.repo)
        self.gateway = DummyGateway()
        self.submission_service = SubmissionService(
            repository=self.repo, gateway=self.gateway
        )

        self.user_store = MagicMock()
        mock_creds = MagicMock()
        mock_creds.jira_pat = "mock-pat-123"
        mock_creds.pat = "mock-pat-123"
        self.user_store.get = AsyncMock(return_value=mock_creds)
        self.user_store.get_credentials.return_value = mock_creds

    def _make_update(
        self,
        *,
        user_id: int = 100,
        chat_id: int = 200,
        chat_type: str = "private",
        text: str = "",
        callback_data: str | None = None,
        message_id: int = 50,
    ) -> tuple[Update, MagicMock]:
        user = User(id=user_id, first_name="Test", is_bot=False)
        chat = Chat(id=chat_id, type=chat_type)

        msg = AsyncMock(spec=Message)
        msg.message_id = message_id
        msg.text = text
        msg.caption = None
        msg.message_thread_id = None

        update = MagicMock(spec=Update)
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = msg

        if callback_data is not None:
            cb_query = AsyncMock()
            cb_query.data = callback_data
            cb_query.message = msg
            update.callback_query = cb_query
        else:
            update.callback_query = None

        return update, msg

    async def test_journey_manual_create_to_review(self) -> None:
        update, msg = self._make_update(text="/create Bug in login form")
        context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        context.args = ["Bug", "in", "login", "form"]
        context.user_data = {}

        preview_msg = AsyncMock(spec=Message)
        preview_msg.message_id = 999
        msg.reply_html.return_value = preview_msg

        await handle_manual_create(
            update,
            context,
            workflow_service=self.workflow_service,
            callback_service=self.callback_service,
        )

        draft_id = context.user_data.get("active_draft_id")
        self.assertIsNotNone(draft_id)
        self.assertEqual(context.user_data.get("active_draft_revision"), 1)

        draft = await self.repo.get_by_id(draft_id)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.state, DraftState.REVIEW)
        self.assertEqual(draft.template.summary, "Bug in login form")

    async def test_journey_toggle_type_and_old_token_invalidation(self) -> None:
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate(
                project_key="TW",
                issue_type="Task",
                summary="Toggle test",
                description="Desc",
                priority="Medium",
            ),
        )

        issued_rev1 = await self.callback_service.issue_preview_buttons(
            draft,
            actions=(
                CallbackAction.CONFIRM,
                CallbackAction.TOGGLE_TYPE,
                CallbackAction.CANCEL,
            ),
            preview_message_id=50,
        )

        ttyp_data_rev1 = issued_rev1[CallbackAction.TOGGLE_TYPE].callback_data
        cfm_data_rev1 = issued_rev1[CallbackAction.CONFIRM].callback_data

        update_toggle, msg_toggle = self._make_update(callback_data=ttyp_data_rev1)
        context = MagicMock()

        await handle_callback_query(
            update_toggle,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
        )

        draft_rev2 = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(draft_rev2.revision, 2)
        self.assertEqual(draft_rev2.template.issue_type, "Bug")

        # Now simulate user or attacker re-clicking old CONFIRM callback from rev 1
        update_old_cfm, msg_old_cfm = self._make_update(callback_data=cfm_data_rev1)
        await handle_callback_query(
            update_old_cfm,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
            submission_service=self.submission_service,
            user_store=self.user_store,
        )

        update_old_cfm.callback_query.answer.assert_called()
        self.assertIn("過期", update_old_cfm.callback_query.answer.call_args[0][0])
        draft_after = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(draft_after.state, DraftState.REVIEW)
        self.assertEqual(draft_after.revision, 2)

    async def test_journey_text_field_edit(self) -> None:
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate(
                project_key="TW",
                issue_type="Task",
                summary="Old Summary",
                description="Desc",
                priority="Medium",
            ),
        )

        update, msg = self._make_update(text="Updated Summary Text")
        context = MagicMock()
        context.user_data = {
            "active_draft_id": draft.draft_id,
            "active_draft_revision": 1,
        }

        await handle_draft_reply_text(
            update,
            context,
            workflow_service=self.workflow_service,
            callback_service=self.callback_service,
        )

        updated_draft = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(updated_draft.revision, 2)
        self.assertEqual(updated_draft.template.summary, "Updated Summary Text")

    async def test_journey_confirm_submit_to_published_issue(self) -> None:
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate(
                project_key="TW",
                issue_type="Task",
                summary="Submit test",
                description="Desc",
                priority="High",
            ),
        )

        issued = await self.callback_service.issue_preview_buttons(
            draft,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=50,
        )
        cfm_data = issued[CallbackAction.CONFIRM].callback_data

        update, msg = self._make_update(callback_data=cfm_data)
        context = MagicMock()

        await handle_callback_query(
            update,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
            submission_service=self.submission_service,
            user_store=self.user_store,
        )

        final_draft = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(final_draft.state, DraftState.CREATED)
        self.assertIsNotNone(final_draft.published_issue)
        self.assertEqual(final_draft.published_issue.issue_key, "TW-100")

    async def test_journey_cancel_draft(self) -> None:
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate(
                project_key="TW",
                issue_type="Task",
                summary="Cancel test",
                description="Desc",
                priority="Low",
            ),
        )

        issued = await self.callback_service.issue_preview_buttons(
            draft,
            actions=(CallbackAction.CANCEL,),
            preview_message_id=50,
        )
        cnl_data = issued[CallbackAction.CANCEL].callback_data

        update, msg = self._make_update(callback_data=cnl_data)
        context = MagicMock()

        await handle_callback_query(
            update,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
        )

        cancelled_draft = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(cancelled_draft.state, DraftState.CANCELLED)

    async def test_journey_retryable_submission_and_retry(self) -> None:
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate(
                project_key="TW",
                issue_type="Task",
                summary="Retry test",
                description="Desc",
                priority="Medium",
            ),
        )

        issued = await self.callback_service.issue_preview_buttons(
            draft, actions=(CallbackAction.CONFIRM,), preview_message_id=50
        )
        cfm_data = issued[CallbackAction.CONFIRM].callback_data

        self.gateway.should_fail_retryable = True

        update, msg = self._make_update(callback_data=cfm_data)
        context = MagicMock()

        await handle_callback_query(
            update,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
            submission_service=self.submission_service,
            user_store=self.user_store,
        )

        retryable_draft = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(retryable_draft.state, DraftState.SUBMISSION_RETRYABLE)

        self.gateway.should_fail_retryable = False

        retry_issued = await self.callback_service.issue_preview_buttons(
            retryable_draft, actions=(CallbackAction.RETRY,), preview_message_id=50
        )
        rty_data = retry_issued[CallbackAction.RETRY].callback_data

        update_retry, _ = self._make_update(callback_data=rty_data)
        await handle_callback_query(
            update_retry,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
            submission_service=self.submission_service,
            user_store=self.user_store,
        )

        final_draft = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(final_draft.state, DraftState.CREATED)

    async def test_journey_security_guards(self) -> None:
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate(
                project_key="TW",
                issue_type="Task",
                summary="Security test",
                description="Desc",
                priority="Medium",
            ),
        )

        issued = await self.callback_service.issue_preview_buttons(
            draft, actions=(CallbackAction.CONFIRM,), preview_message_id=50
        )
        cfm_data = issued[CallbackAction.CONFIRM].callback_data

        # Foreign user attempt (user_id = 999 instead of 100)
        update_foreign, _ = self._make_update(
            user_id=999, chat_id=200, callback_data=cfm_data
        )
        context = MagicMock()
        await handle_callback_query(
            update_foreign,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
        )
        update_foreign.callback_query.answer.assert_called()
        self.assertIn(
            "無法操作其他人的工單草稿", update_foreign.callback_query.answer.call_args[0][0]
        )

        # Group chat attempt (chat_type = "group")
        update_group, _ = self._make_update(
            user_id=100, chat_id=200, chat_type="group", callback_data=cfm_data
        )
        await handle_callback_query(
            update_group,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
        )
        self.assertIn("私聊", update_group.callback_query.answer.call_args[0][0])

    async def test_allowlist_enforcement_in_draft_handlers(self) -> None:
        """Allowlist restricts non-allowed actors across all UI handler entry points."""
        allowlist = frozenset({100})

        # Manual create - denied for 999
        update_denied, msg_denied = self._make_update(user_id=999, text="/create bug")
        ctx = MagicMock()
        ctx.args = ["bug"]
        ctx.user_data = {}
        await handle_manual_create(
            update_denied,
            ctx,
            workflow_service=self.workflow_service,
            callback_service=self.callback_service,
            allowed_user_ids=allowlist,
        )
        self.assertIsNone(ctx.user_data.get("active_draft_id"))
        msg_denied.reply_html.assert_called_once()
        self.assertIn("您沒有權限使用此機器人", msg_denied.reply_html.call_args[0][0])

        # Forward intake - denied for 999
        update_intake_denied, msg_intake_denied = self._make_update(user_id=999, text="forwarded message")
        ctx_intake = MagicMock()
        ctx_intake.user_data = {}
        mock_intake_svc = MagicMock()
        await handle_forward_intake(
            update_intake_denied,
            ctx_intake,
            workflow_service=self.workflow_service,
            intake_service=mock_intake_svc,
            callback_service=self.callback_service,
            allowed_user_ids=allowlist,
        )
        self.assertIsNone(ctx_intake.user_data.get("active_draft_id"))
        msg_intake_denied.reply_html.assert_called_once()
        self.assertIn("您沒有權限使用此機器人", msg_intake_denied.reply_html.call_args[0][0])

        # Draft reply text - denied for 999
        update_text_denied, msg_text_denied = self._make_update(user_id=999, text="reply text")
        ctx_text = MagicMock()
        ctx_text.user_data = {"active_draft_id": "draft-1", "active_draft_revision": 1}
        await handle_draft_reply_text(
            update_text_denied,
            ctx_text,
            workflow_service=self.workflow_service,
            callback_service=self.callback_service,
            allowed_user_ids=allowlist,
        )
        msg_text_denied.reply_html.assert_called_once()
        self.assertIn("您沒有權限使用此機器人", msg_text_denied.reply_html.call_args[0][0])

        # Callback query - denied for 999
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate("TW", "Task", "Summary", "Desc", "Medium"),
        )
        issued = await self.callback_service.issue_preview_buttons(
            draft, actions=(CallbackAction.CONFIRM,), preview_message_id=50
        )
        cfm_data = issued[CallbackAction.CONFIRM].callback_data
        update_cb_denied, _ = self._make_update(user_id=999, callback_data=cfm_data)
        await handle_callback_query(
            update_cb_denied,
            MagicMock(),
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
            allowed_user_ids=allowlist,
        )
        update_cb_denied.callback_query.answer.assert_called_once()
        self.assertIn("您沒有權限使用此機器人", update_cb_denied.callback_query.answer.call_args[0][0])

    async def test_real_user_store_confirm_seam(self) -> None:
        """Confirm uses async UserStore.get and retrieves jira_pat."""
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate("TW", "Task", "Real store test", "Desc", "High"),
        )
        issued = await self.callback_service.issue_preview_buttons(
            draft, actions=(CallbackAction.CONFIRM,), preview_message_id=50
        )
        cfm_data = issued[CallbackAction.CONFIRM].callback_data

        real_user_store = MagicMock()
        mock_creds = MagicMock()
        mock_creds.jira_pat = "real-user-store-pat-999"
        del mock_creds.pat  # Ensure it uses jira_pat, not old .pat
        real_user_store.get = AsyncMock(return_value=mock_creds)

        update, _ = self._make_update(user_id=100, callback_data=cfm_data)
        context = MagicMock()

        await handle_callback_query(
            update,
            context,
            callback_service=self.callback_service,
            workflow_service=self.workflow_service,
            submission_service=self.submission_service,
            user_store=real_user_store,
        )

        real_user_store.get.assert_awaited_once_with(100)
        final_draft = await self.repo.get_by_id(draft.draft_id)
        self.assertEqual(final_draft.state, DraftState.CREATED)

    async def test_redacted_attachment_failure_logging(self) -> None:
        """Attachment upload failure logs event code and exception type only, redacting sensitive text."""
        draft = await self.workflow_service.create_manual_draft(
            owner_id=100,
            chat_id=200,
            template=JiraTaskTemplate("TW", "Task", "Attachment log test", "Desc", "Medium"),
        )
        # Attach dummy attachment to trigger upload_pending
        attachment = Attachment(
            file_id="AgADBAADSECRET_FILE_ID",
            file_unique_id="unique-secret-1",
            file_name="photo.jpg",
            file_size=12,
        )
        draft_with_att = replace(draft, attachments=(attachment,))
        await self.repo.save(draft_with_att)

        issued = await self.callback_service.issue_preview_buttons(
            draft_with_att, actions=(CallbackAction.CONFIRM,), preview_message_id=50
        )
        cfm_data = issued[CallbackAction.CONFIRM].callback_data

        mock_att_svc = MagicMock()
        mock_att_svc.upload_pending = AsyncMock(
            side_effect=RuntimeError("Leaked PAT=secret-123 file_id=AgADBAADSECRET http://jira.invalid")
        )

        update, _ = self._make_update(user_id=100, callback_data=cfm_data)

        with unittest.mock.patch("dztgbot.ui.handlers.callbacks.LOGGER") as mock_logger:
            await handle_callback_query(
                update,
                MagicMock(),
                callback_service=self.callback_service,
                workflow_service=self.workflow_service,
                submission_service=self.submission_service,
                attachment_service=mock_att_svc,
                user_store=self.user_store,
            )
            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args[0]
            log_str = call_args[0] % call_args[1:] if len(call_args) > 1 else call_args[0]
            self.assertIn("RuntimeError", log_str)
            self.assertNotIn("secret-123", log_str)
            self.assertNotIn("AgADBAADSECRET", log_str)
            self.assertNotIn("jira.invalid", log_str)


if __name__ == "__main__":
    unittest.main()
