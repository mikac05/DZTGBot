"""Phase 6 Task P6-G — integrated security verification through composed seams.

Exercises private-only gates, PAT-only auth, credential store failure preservation,
corrupt-store fail-safe recovery, group non-disclosure, logout accuracy, and
provider/privacy redaction via real handlers + services + temporary local storage.

No live Telegram, Gemini, Jira, VPN, or network I/O.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from telegram.ext import ConversationHandler

from dztgbot.__main__ import handle_application_error
from dztgbot.admin import build_admin_handlers
from dztgbot.config import Settings
from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.errors import (
    SafeErrorCode,
    classify_definite_mutation_failure,
    ErrorKind,
    Operation,
)
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Attachment, Draft, JiraTaskTemplate, PublishedIssue
from dztgbot.domain.policy import (
    DenialCode,
    logout_revokes_remote_pat,
    require_allowed_user,
    user_message_for_denial,
)
from dztgbot.infrastructure.gemini_gateway import GeminiGateway, GeminiGatewayError
from dztgbot.infrastructure.jira_gateway import JiraGateway, JiraGatewayError
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.jira_auth import AUTH_STARTED_AT_KEY, AWAITING_PAT, build_auth_handlers
from dztgbot.jira_client import JiraClientError, JiraUser
from dztgbot.rules import RulesStore
from dztgbot.services.attachment_service import AttachmentService
from dztgbot.services.callback_service import CallbackService
from dztgbot.services.submission_service import SubmissionService
from dztgbot.services.workflow_service import WorkflowService
from dztgbot.ui.handlers.callbacks import handle_callback_query
from dztgbot.ui.handlers.drafts import handle_manual_create
from dztgbot.ui.rendering import render_private_only_warning
from dztgbot.user_store import JiraCredentials, UserStore
from dztgbot.vpn import VpnState
from tests.support.security_fakes import (
    TEST_ONLY_BASIC_SHAPE,
    TEST_ONLY_COOKIE_SHAPE,
    TEST_ONLY_PASSWORD_SHAPE,
    TEST_ONLY_PAT,
    minimal_env,
)


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)
OWNER_ID = 1001
CHAT_ID = 1001
PREVIEW_MESSAGE_ID = 9001


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
        return f"sec-draft-{self._n}"


class FakeVpn:
    def __init__(self) -> None:
        self.status_calls = 0
        self.start_calls = 0

    async def status(self) -> SimpleNamespace:
        self.status_calls += 1
        return SimpleNamespace(
            state=VpnState.UP if hasattr(VpnState, "UP") else "up",
            message="VPN is up (endpoint=vpn.secret.example path=/etc/nm)",
        )

    async def start(self) -> SimpleNamespace:
        self.start_calls += 1
        return SimpleNamespace(
            state=VpnState.UP if hasattr(VpnState, "UP") else "up",
            message="VPN start accepted (endpoint=vpn.secret.example)",
        )


class SecretLeakingGateway:
    """Submission gateway that raises a classified, privacy-safe provider failure."""

    def __init__(self) -> None:
        self.create_calls = 0

    async def create_issue(self, template, pat, idempotency_key=None):  # type: ignore[no-untyped-def]
        self.create_calls += 1
        raise JiraGatewayError(
            classify_definite_mutation_failure(
                operation=Operation.JIRA_CREATE,
                kind=ErrorKind.PROVIDER_REJECTION,
                safe_code=SafeErrorCode.PROVIDER_REJECTED,
            )
        )

    async def update_issue(self, issue_key, template, pat):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def find_by_request_hash(self, project_key, request_hash, pat):  # type: ignore[no-untyped-def]
        return ()

    async def get_issue(self, issue_key, pat):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class SuccessGateway:
    async def create_issue(self, template, pat, idempotency_key=None):  # type: ignore[no-untyped-def]
        return PublishedIssue(
            issue_key="BOT-77",
            issue_id="77",
            issue_url="https://jira.invalid/browse/BOT-77",
            published_at=NOW,
        )

    async def update_issue(self, issue_key, template, pat):  # type: ignore[no-untyped-def]
        return None

    async def find_by_request_hash(self, project_key, request_hash, pat):  # type: ignore[no-untyped-def]
        return ()

    async def get_issue(self, issue_key, pat):  # type: ignore[no-untyped-def]
        return SimpleNamespace(fields={})


class LeakyAttachmentGateway:
    async def upload_attachment(
        self, issue_key, filename, content, mime_type, pat  # type: ignore[no-untyped-def]
    ) -> str:
        raise RuntimeError(
            f"Jira body: attachment failed for file_id=AgADBAADSECRET pat={TEST_ONLY_PAT}"
        )


class LeakyAttachmentLoader:
    async def load(self, file_id: str):  # type: ignore[no-untyped-def]
        from dztgbot.services.attachment_service import AttachmentContent

        if "SECRET" in file_id:
            raise RuntimeError(
                f"Telegram download failed file_id={file_id} pat={TEST_ONLY_PAT}"
            )
        return AttachmentContent(b"image-bytes", "photo.jpg", "image/jpeg")


def _user(user_id: int = OWNER_ID) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, full_name="Sec User", username="sec_user")


def _private_chat(chat_id: int = CHAT_ID) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="private", title=None)


def _group_chat(chat_id: int = -5001) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="supergroup", title="Ops Group")


def _message(
    *,
    text: str | None = None,
    chat: SimpleNamespace | None = None,
    user: SimpleNamespace | None = None,
    message_id: int = 42,
    delete_ok: bool = True,
    message_thread_id: int | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.message_id = message_id
    msg.message_thread_id = message_thread_id
    msg.chat = chat
    msg.from_user = user
    msg.delete = AsyncMock()
    if not delete_ok:
        msg.delete.side_effect = RuntimeError("Telegram forbidden")
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    preview = MagicMock()
    preview.message_id = PREVIEW_MESSAGE_ID
    preview.edit_text = AsyncMock()
    preview.edit_reply_markup = AsyncMock()
    msg.reply_html.return_value = preview
    return msg


def _update(
    *,
    text: str | None = None,
    chat: SimpleNamespace | None = None,
    user: SimpleNamespace | None = None,
    message_id: int = 42,
    delete_ok: bool = True,
    callback_data: str | None = None,
    message_thread_id: int | None = None,
) -> tuple[MagicMock, MagicMock]:
    chat = chat or _private_chat()
    user = user or _user()
    message = _message(
        text=text,
        chat=chat,
        user=user,
        message_id=message_id,
        delete_ok=delete_ok,
        message_thread_id=message_thread_id,
    )
    update = MagicMock()
    update.effective_message = message
    update.effective_user = user
    update.effective_chat = chat
    if callback_data is not None:
        query = AsyncMock()
        query.data = callback_data
        query.message = message
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
    else:
        update.callback_query = None
    return update, message


def _context(*, started_at: datetime | None = None) -> MagicMock:
    ctx = MagicMock()
    data: dict = {}
    if started_at is not None:
        data[AUTH_STARTED_AT_KEY] = started_at
    ctx.user_data = data
    ctx.args = []
    return ctx


class _FakeChat:
    def __init__(self, chat_id: int = CHAT_ID, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type
        self.send_message = AsyncMock()


class IntegratedSecurityFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "workflow.sqlite3"
        self.creds_path = root / "creds.json"
        self.rules_path = root / "rules.txt"
        self.rules_path.write_text(
            "SECRET_RUNTIME_RULES_BODY\nDo not disclose in groups.\n",
            encoding="utf-8",
        )

        self.repository = SQLiteWorkflowRepository(self.db_path, enable_wal=False)
        await self.repository.initialize()
        self.clock = FixedClock()
        self.ids = SequenceIds()
        self.workflow = WorkflowService(
            repository=self.repository,
            clock=self.clock,
            id_generator=self.ids,
        )
        self.callbacks = CallbackService(
            drafts=self.repository,
            tokens=self.repository,
            clock=self.clock,
        )
        self.user_store = UserStore(self.creds_path)
        await self.user_store.initialize()

        self.jira = MagicMock()
        self.jira.validate_credentials = AsyncMock(
            return_value=JiraUser(
                username="jira.user",
                display_name="Jira User",
                email=None,
            )
        )
        (
            self.auth_conv,
            self.start_handler,
            self.logout_handler,
            self.help_handler,
        ) = build_auth_handlers(
            self.user_store,
            self.jira,
            "https://jira.test.example.com",
        )
        self.auth_entry = self.auth_conv.entry_points[0].callback
        self.receive_pat = self.auth_conv.states[AWAITING_PAT][0].callback
        self.logout_cmd = self.logout_handler.callback
        self.start_cmd = self.start_handler.callback

        self.rules_store = RulesStore(self.rules_path)
        await self.rules_store.initialize()
        self.vpn = FakeVpn()
        self.admin_handlers = {
            next(iter(h.commands)): h.callback  # type: ignore[attr-defined]
            for h in build_admin_handlers(
                self.rules_store, frozenset({OWNER_ID}), self.vpn  # type: ignore[arg-type]
            )
        }

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self._tmp.cleanup()

    async def _store_pat(self, user_id: int = OWNER_ID, pat: str = TEST_ONLY_PAT) -> None:
        await self.user_store.store(
            user_id,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=pat,
            ),
        )

    async def _issue_review_draft(
        self,
        *,
        owner_id: int = OWNER_ID,
        chat_id: int = CHAT_ID,
        preview_message_id: int = PREVIEW_MESSAGE_ID,
        attachments: tuple[Attachment, ...] = (),
    ) -> tuple[Draft, str]:
        draft = await self.workflow.create_manual_draft(
            owner_id=owner_id,
            chat_id=chat_id,
            template=JiraTaskTemplate(
                "BOT", "Task", "integrated summary", "integrated description", "Medium"
            ),
        )
        if attachments:
            draft = Draft(
                draft_id=draft.draft_id,
                owner_id=draft.owner_id,
                chat_id=draft.chat_id,
                message_thread_id=draft.message_thread_id,
                state=draft.state,
                revision=draft.revision + 1,
                template=draft.template,
                source_messages=draft.source_messages,
                attachments=attachments,
                created_at=draft.created_at,
                updated_at=draft.updated_at,
                published_issue=draft.published_issue,
                last_error=draft.last_error,
            )
            await self.repository.save(draft)
        issued = await self.callbacks.issue_preview_buttons(
            draft,
            actions=(CallbackAction.CONFIRM, CallbackAction.CANCEL),
            preview_message_id=preview_message_id,
        )
        return draft, issued[CallbackAction.CONFIRM].callback_data


class PrivateOnlyIntegratedTests(IntegratedSecurityFixture):
    async def test_group_manual_create_is_denied_without_persisting_draft(self) -> None:
        update, message = _update(text="/new secret ticket", chat=_group_chat())
        ctx = _context()
        await handle_manual_create(
            update,
            ctx,
            workflow_service=self.workflow,
            callback_service=self.callbacks,
        )
        message.reply_html.assert_awaited()
        body = message.reply_html.await_args.args[0]
        self.assertEqual(body, render_private_only_warning())
        self.assertIsNone(ctx.user_data.get("active_draft_id"))
        # No draft should exist in SQLite authority.
        listed = []
        for draft_id in ("sec-draft-1", "sec-draft-2"):
            found = await self.repository.get_by_id(draft_id)
            if found is not None:
                listed.append(found)
        self.assertEqual(listed, [])

    async def test_group_auth_and_admin_disclose_no_identity_rules_or_vpn(self) -> None:
        await self._store_pat()
        group = _group_chat()

        update_auth, msg_auth = _update(text="/auth", chat=group)
        result = await self.auth_entry(update_auth, _context())
        self.assertEqual(result, ConversationHandler.END)
        auth_body = msg_auth.reply_text.await_args.args[0]
        self.assertIn("私聊", auth_body)
        self.assertNotIn("Jira User", auth_body)
        self.assertNotIn(TEST_ONLY_PAT, auth_body)

        update_start, msg_start = _update(text="/start", chat=group)
        await self.start_cmd(update_start, _context())
        start_body = msg_start.reply_text.await_args.args[0]
        self.assertIn("私聊", start_body)
        self.assertNotIn("Jira User", start_body)
        self.assertNotIn("jira.user", start_body)
        self.assertNotIn(TEST_ONLY_PAT, start_body)

        update_rules, msg_rules = _update(text="/rules", chat=group, user=_user(OWNER_ID))
        await self.admin_handlers["rules"](update_rules, MagicMock())
        rules_body = msg_rules.reply_text.await_args.args[0]
        self.assertEqual(rules_body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("SECRET_RUNTIME_RULES_BODY", rules_body)

        update_vpn, msg_vpn = _update(text="/vpn", chat=group, user=_user(OWNER_ID))
        await self.admin_handlers["vpn"](update_vpn, MagicMock())
        vpn_body = msg_vpn.reply_text.await_args.args[0]
        self.assertEqual(vpn_body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("endpoint=", vpn_body)
        self.assertNotIn("vpn.secret", vpn_body)
        self.assertEqual(self.vpn.status_calls, 0)

        update_logout, msg_logout = _update(text="/logout", chat=group)
        await self.logout_cmd(update_logout, _context())
        logout_body = msg_logout.reply_text.await_args.args[0]
        self.assertIn("私聊", logout_body)
        self.assertIsNotNone(await self.user_store.get(OWNER_ID))
        self.assertNotIn("Jira User", logout_body)


class PatOnlyAndCredentialStoreTests(IntegratedSecurityFixture):
    async def test_pat_only_accepts_pat_and_rejects_password_cookie_basic(self) -> None:
        chat = _FakeChat()
        update, message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)
        self.assertEqual(result, ConversationHandler.END)
        stored = await self.user_store.get(OWNER_ID)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.jira_pat, TEST_ONLY_PAT)
        message.delete.assert_awaited()
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)

        for raw in (
            TEST_ONLY_PASSWORD_SHAPE,
            TEST_ONLY_COOKIE_SHAPE,
            TEST_ONLY_BASIC_SHAPE,
        ):
            with self.subTest(raw=raw):
                self.jira.validate_credentials.reset_mock()
                chat2 = _FakeChat(chat_id=2002)
                update2, message2 = _update(text=raw, user=_user(2002), chat=_private_chat(2002))
                update2.effective_chat = chat2
                ctx2 = _context(started_at=datetime.now(timezone.utc))
                result2 = await self.receive_pat(update2, ctx2)
                self.assertEqual(result2, AWAITING_PAT)
                self.jira.validate_credentials.assert_not_awaited()
                self.assertIsNone(await self.user_store.get(2002))
                message2.delete.assert_awaited()
                sent = chat2.send_message.await_args.args[0]
                self.assertEqual(
                    sent,
                    user_message_for_denial(DenialCode.CREDENTIAL_FORMAT_REJECTED),
                )
                self.assertNotIn(raw, sent)

    async def test_store_write_failure_preserves_prior_state_through_auth_handler(
        self,
    ) -> None:
        await self._store_pat(pat="TEST_ONLY_PRIOR_PAT_VALUE_NOT_REAL")
        chat = _FakeChat()
        update, _message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        ctx = _context(started_at=datetime.now(timezone.utc))

        with patch.object(
            self.user_store, "store", AsyncMock(side_effect=OSError("disk full"))
        ):
            result = await self.receive_pat(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        prior = await self.user_store.get(OWNER_ID)
        self.assertIsNotNone(prior)
        assert prior is not None
        self.assertEqual(prior.jira_pat, "TEST_ONLY_PRIOR_PAT_VALUE_NOT_REAL")
        body = status.edit_text.await_args.args[0]
        self.assertIn("無法安全儲存", body)
        self.assertNotIn(TEST_ONLY_PAT, body)
        self.assertNotIn("TEST_ONLY_PRIOR_PAT_VALUE_NOT_REAL", body)

    async def test_store_remove_failure_preserves_credentials_on_logout(self) -> None:
        await self._store_pat()
        update, message = _update(text="/logout")
        with patch.object(
            self.user_store, "remove", AsyncMock(side_effect=OSError("disk full"))
        ):
            await self.logout_cmd(update, _context())
        self.assertIsNotNone(await self.user_store.get(OWNER_ID))
        body = message.reply_text.await_args.args[0]
        self.assertIn("解綁失敗", body)
        self.assertNotIn(TEST_ONLY_PAT, body)
        self.assertNotIn("撤銷", body)

    async def test_corrupt_primary_store_recovers_from_previous_copy(self) -> None:
        await self._store_pat(pat="TEST_ONLY_V1_PAT")
        await self._store_pat(pat="TEST_ONLY_V2_PAT")
        prev = self.creds_path.with_name(self.creds_path.name + ".prev")
        self.assertTrue(prev.exists())
        self.creds_path.write_text("{not-json-corrupt", encoding="utf-8")

        recovered = UserStore(self.creds_path)
        await recovered.initialize()
        got = await recovered.get(OWNER_ID)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertIn(got.jira_pat, {"TEST_ONLY_V1_PAT", "TEST_ONLY_V2_PAT"})
        # Quarantine or restored primary must exist; secrets must not appear in logs.
        with self.assertLogs("dztgbot.user_store", level="WARNING") as captured:
            # Force a second initialize path that logs quarantine when primary is bad.
            broken = UserStore(self.creds_path)
            # Re-corrupt if recovery rewrote primary.
            if self.creds_path.exists() and self.creds_path.read_text(encoding="utf-8").startswith("{"):
                self.creds_path.write_text("{still-bad", encoding="utf-8")
            await broken.initialize()
        combined = "\n".join(captured.output)
        self.assertNotIn("TEST_ONLY_V1_PAT", combined)
        self.assertNotIn("TEST_ONLY_V2_PAT", combined)
        self.assertNotIn(TEST_ONLY_PAT, combined)

    async def test_logout_claims_local_removal_only(self) -> None:
        self.assertFalse(logout_revokes_remote_pat())
        await self._store_pat()
        update, message = _update(text="/logout")
        await self.logout_cmd(update, _context())
        self.assertIsNone(await self.user_store.get(OWNER_ID))
        body = message.reply_text.await_args.args[0]
        self.assertIn("本機", body)
        self.assertIn("不會撤銷", body)
        self.assertNotIn(TEST_ONLY_PAT, body)
        self.assertNotIn("https://jira", body)


class PrivacyAndProviderRedactionTests(IntegratedSecurityFixture):
    async def test_auth_validation_failure_redacts_provider_and_pat_from_logs(
        self,
    ) -> None:
        chat = _FakeChat()
        update, _message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        self.jira.validate_credentials = AsyncMock(
            side_effect=JiraClientError(
                "secret provider body Authorization=Bearer "
                f"{TEST_ONLY_PAT} file_id=AgADBAADSECRET"
            )
        )
        ctx = _context(started_at=datetime.now(timezone.utc))

        with self.assertLogs("dztgbot.jira_auth", level="WARNING") as captured:
            result = await self.receive_pat(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        combined = "\n".join(captured.output)
        self.assertNotIn(TEST_ONLY_PAT, combined)
        self.assertNotIn("secret provider body", combined)
        self.assertNotIn("AgADBAADSECRET", combined)
        user_text = status.edit_text.await_args.args[0]
        self.assertNotIn(TEST_ONLY_PAT, user_text)
        self.assertNotIn("secret provider body", user_text)

    async def test_global_error_handler_and_gateway_errors_are_privacy_safe(self) -> None:
        secret = (
            f"leaked {TEST_ONLY_PAT} j1:cfm:deadbeef callback token "
            "message text BODY file_id=AgADBAADSECRET "
            "https://jira.secret.example/rest vpn.endpoint.private"
        )
        context = SimpleNamespace(error=ValueError(secret))
        with patch("dztgbot.__main__.LOGGER") as logger:
            await handle_application_error({"text": secret}, context)  # type: ignore[arg-type]
            logger.error.assert_called_once()
            call_args = logger.error.call_args[0]
            rendered = call_args[0] % call_args[1:] if len(call_args) > 1 else call_args[0]
            self.assertIn("ValueError", rendered)
            self.assertNotIn(TEST_ONLY_PAT, rendered)
            self.assertNotIn("message text BODY", rendered)
            self.assertNotIn("AgADBAADSECRET", rendered)
            self.assertNotIn("j1:cfm:", rendered)
            self.assertNotIn("vpn.endpoint", rendered)

        async def jira_handler(request: httpx.Request) -> httpx.Response:
            self.assertNotIn(TEST_ONLY_PAT, str(request.url))
            return httpx.Response(
                400,
                json={
                    "errorMessages": [f"raw jira body with {TEST_ONLY_PAT}"],
                    "errors": {"summary": f"bad field {TEST_ONLY_PAT}"},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(jira_handler))
        gateway = JiraGateway(base_url="https://jira.invalid", client=client)
        with self.assertRaises(JiraGatewayError) as ctx:
            await gateway.test_credential(TEST_ONLY_PAT)
        error = ctx.exception
        self.assertEqual(str(error), SafeErrorCode.PROVIDER_REJECTED.value)
        self.assertNotIn(TEST_ONLY_PAT, str(error))
        self.assertNotIn("raw jira body", str(error))
        await client.aclose()

        class Boom:
            async def generate_content(self, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError(
                    f"gemini body {TEST_ONLY_PAT} file_id=AgADBAADSECRET"
                )

        gemini = GeminiGateway(
            client=SimpleNamespace(aio=SimpleNamespace(models=Boom())),
            max_retries=0,
            backoff_seconds=0,
        )
        with self.assertRaises(GeminiGatewayError) as gctx:
            from dztgbot.domain.models import MediaKind, SourceMessageRef

            await gemini.analyze_messages(
                [
                    SourceMessageRef(
                        1, 1, 1, f"secret message {TEST_ONLY_PAT}", MediaKind.TEXT, NOW
                    )
                ],
                "rules",
                "BOT",
            )
        self.assertEqual(str(gctx.exception), SafeErrorCode.PROVIDER_REJECTED.value)
        self.assertNotIn(TEST_ONLY_PAT, str(gctx.exception))

    async def test_submission_failure_persists_and_renders_only_safe_error_codes(
        self,
    ) -> None:
        await self._store_pat()
        draft, callback_data = await self._issue_review_draft()
        submission = SubmissionService(self.repository, SecretLeakingGateway())
        update, _message = _update(callback_data=callback_data)
        update.callback_query.message.message_id = PREVIEW_MESSAGE_ID
        update.effective_user = _user(OWNER_ID)
        update.effective_chat = _private_chat(CHAT_ID)

        class SyncPatAdapter:
            def get_credentials(self, user_id: int):  # type: ignore[no-untyped-def]
                return SimpleNamespace(pat=TEST_ONLY_PAT)

        await handle_callback_query(
            update,
            _context(),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
            submission_service=submission,
            user_store=SyncPatAdapter(),  # type: ignore[arg-type]
        )

        final = await self.repository.get_by_id(draft.draft_id)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.state, DraftState.SUBMISSION_RETRYABLE)
        self.assertEqual(final.last_error, SafeErrorCode.PROVIDER_REJECTED.value)
        self.assertNotIn(TEST_ONLY_PAT, final.last_error or "")
        edit_calls = update.callback_query.edit_message_text.await_args_list
        for call in edit_calls:
            text = call.args[0] if call.args else ""
            self.assertNotIn(TEST_ONLY_PAT, text)
            self.assertNotIn("Authorization", text)

    async def test_attachment_failures_store_only_safe_codes_not_provider_or_file_ids(
        self,
    ) -> None:
        """Attachment integration seam must not persist Telegram file IDs or provider text."""

        attachments = (
            Attachment(
                file_id="AgADBAADSECRET_FILE_ID",
                file_unique_id="unique-secret-1",
                file_name="photo.jpg",
                file_size=12,
            ),
        )
        draft, _callback_data = await self._issue_review_draft(attachments=attachments)
        created = await self.repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.SUBMITTING
        )
        created = await self.repository.compare_and_swap_state(
            created.draft_id, created.revision, DraftState.CREATED
        )
        issue = PublishedIssue(
            issue_key="BOT-77",
            issue_id="77",
            issue_url="https://jira.invalid/browse/BOT-77",
            published_at=NOW,
        )
        await self.repository.store_published_issue(draft.draft_id, issue)
        created = await self.repository.get_by_id(draft.draft_id)
        assert created is not None

        service = AttachmentService(
            self.repository,
            LeakyAttachmentGateway(),
            LeakyAttachmentLoader(),
        )
        result = await service.upload_pending(draft.draft_id, TEST_ONLY_PAT)
        self.assertGreaterEqual(result.failed, 1)
        records = await self.repository.list_attachments(draft.draft_id)
        for record in records:
            code = getattr(record, "last_error_code", None)
            if code is not None:
                self.assertNotIn(TEST_ONLY_PAT, code)
                self.assertNotIn("AgADBAADSECRET", code)
                self.assertNotIn("Jira body", code)
                self.assertNotIn("Telegram download", code)
        # Draft last_error must remain a safe code when partial.
        final = await self.repository.get_by_id(draft.draft_id)
        assert final is not None
        if final.last_error:
            self.assertNotIn(TEST_ONLY_PAT, final.last_error)
            self.assertNotIn("AgADBAADSECRET", final.last_error)


class AllowedUserCompositionTests(IntegratedSecurityFixture):
    async def test_allowed_user_policy_is_enforced_through_composed_manual_create(
        self,
    ) -> None:
        """Composition defect probe: allowlist must gate composed workflow entry.

        Policy + config exist; handlers currently omit the check.
        Owning interfaces if this fails:
        - ``src/dztgbot/ui/handlers/drafts.py`` (``handle_manual_create``)
        - ``src/dztgbot/jira_auth.py`` (auth entry)
        - ``src/dztgbot/__main__.py`` (does not pass ``telegram_allowed_user_ids``)
        """

        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = minimal_env(rules_path=str(rules))
            env["TELEGRAM_ALLOWED_USER_IDS"] = str(OWNER_ID)
            env["WORKFLOW_DB_PATH"] = str(Path(tmp) / "wf.sqlite3")
            env["USER_CREDENTIALS_PATH"] = str(Path(tmp) / "creds.json")
            env["JIRA_DEFAULT_PROJECT_KEY"] = "BOT"
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.telegram_allowed_user_ids, frozenset({OWNER_ID}))

        allowed = settings.telegram_allowed_user_ids
        self.assertTrue(require_allowed_user(OWNER_ID, allowed).allowed)
        denied = require_allowed_user(9999, allowed)
        self.assertEqual(denied.denial_code, DenialCode.NOT_ALLOWED_USER)
        self.assertEqual(
            user_message_for_denial(DenialCode.NOT_ALLOWED_USER),
            "您沒有權限使用此機器人。",
        )

        # Composed handler without allowlist injection currently creates the draft.
        update, message = _update(
            text="/new not allowed",
            user=_user(9999),
            chat=_private_chat(9999),
        )
        ctx = _context()
        await handle_manual_create(
            update,
            ctx,
            workflow_service=self.workflow,
            callback_service=self.callbacks,
        )
        created_id = ctx.user_data.get("active_draft_id")
        self.assertIsNone(
            created_id,
            msg=(
                "SOURCE BLOCKER: allowlisted policy is not enforced by "
                "handle_manual_create / composition root "
                "(ui/handlers/drafts.py, jira_auth.py, __main__.py)"
            ),
        )
        if message.reply_html.await_count:
            body = message.reply_html.await_args.args[0]
            self.assertNotIn(TEST_ONLY_PAT, body)


class ProductionUserStoreSeamTests(IntegratedSecurityFixture):
    async def test_confirm_uses_real_user_store_pat_without_get_credentials_adapter(
        self,
    ) -> None:
        """Composition defect probe: confirm must load PAT from UserStore.

        Owning interface if this fails: ``src/dztgbot/ui/handlers/callbacks.py``
        (uses ``get_credentials`` / ``.pat`` instead of ``UserStore.get`` / ``jira_pat``).
        """

        await self._store_pat()
        draft, callback_data = await self._issue_review_draft()
        submission = SubmissionService(self.repository, SuccessGateway())
        update, _message = _update(callback_data=callback_data)
        update.callback_query.message.message_id = PREVIEW_MESSAGE_ID

        await handle_callback_query(
            update,
            _context(),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
            submission_service=submission,
            user_store=self.user_store,
        )

        final = await self.repository.get_by_id(draft.draft_id)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(
            final.state,
            DraftState.CREATED,
            msg=(
                "SOURCE BLOCKER: composed confirm cannot load PAT from production "
                "UserStore (ui/handlers/callbacks.py expects get_credentials/.pat; "
                "UserStore exposes async get()/jira_pat)"
            ),
        )
        answers = [
            call.args[0]
            for call in update.callback_query.answer.await_args_list
            if call.args
        ]
        joined = "\n".join(answers)
        self.assertNotIn(TEST_ONLY_PAT, joined)
        self.assertNotIn(
            "請先透過 /auth",
            joined,
            msg="confirm fell back to auth prompt despite stored credentials",
        )


if __name__ == "__main__":
    unittest.main()
