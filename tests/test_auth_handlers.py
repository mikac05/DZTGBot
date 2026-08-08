"""Deterministic auth handler tests (Phase 5 Task P5-G).

Covers PAT-only acceptance/rejection, conversation timeout, credential-message
deletion failure, late menu/ordinary input, logout accuracy, and private-only
identity-bearing /start behavior.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ConversationHandler

from dztgbot.config import DEFAULT_AUTH_TTL_SECONDS
from dztgbot.domain.policy import (
    AUTH_CONVERSATION_TTL,
    DenialCode,
    logout_revokes_remote_pat,
    user_message_for_denial,
)
from dztgbot.jira_auth import (
    AUTH_STARTED_AT_KEY,
    AWAITING_PAT,
    build_auth_handlers,
    get_main_menu_keyboard,
)
from dztgbot.jira_client import JiraClientError, JiraUser
from dztgbot.user_store import JiraCredentials, UserStore
from tests.support.security_fakes import (
    TEST_ONLY_BASIC_SHAPE,
    TEST_ONLY_COOKIE_SHAPE,
    TEST_ONLY_PASSWORD_SHAPE,
    TEST_ONLY_PAT,
)


def _user(user_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, full_name="Test User", username="tester")


def _private_chat(chat_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="private", title=None)


def _group_chat(chat_id: int = -2002) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="supergroup", title="Ops")


def _message(
    *,
    text: str | None = None,
    chat: SimpleNamespace | None = None,
    user: SimpleNamespace | None = None,
    delete_ok: bool = True,
) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.message_id = 42
    msg.delete = AsyncMock()
    if not delete_ok:
        msg.delete.side_effect = RuntimeError("Telegram forbidden")
    msg.reply_text = AsyncMock()
    msg.chat = chat
    msg.from_user = user
    return msg


def _update(
    *,
    text: str | None = None,
    chat: SimpleNamespace | None = None,
    user: SimpleNamespace | None = None,
    delete_ok: bool = True,
) -> tuple[MagicMock, MagicMock]:
    chat = chat or _private_chat()
    user = user or _user()
    message = _message(text=text, chat=chat, user=user, delete_ok=delete_ok)
    update = MagicMock()
    update.effective_message = message
    update.effective_user = user
    update.effective_chat = chat
    return update, message


def _context(*, started_at: datetime | None = None) -> MagicMock:
    ctx = MagicMock()
    data: dict = {}
    if started_at is not None:
        data[AUTH_STARTED_AT_KEY] = started_at
    ctx.user_data = data
    return ctx


class _FakeChat:
    def __init__(self, chat_id: int = 1001, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type
        self.send_message = AsyncMock()


class AuthHandlerFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "creds.json"
        self.store = UserStore(path)
        await self.store.initialize()
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
            self.store,
            self.jira,
            "https://jira.test.example.com",
            auth_ttl_seconds=DEFAULT_AUTH_TTL_SECONDS,
        )
        self.auth_entry = self.auth_conv.entry_points[0].callback
        self.receive_pat = self.auth_conv.states[AWAITING_PAT][0].callback
        self.cancel = self.auth_conv.fallbacks[0].callback
        self.auth_timeout = self.auth_conv.states[ConversationHandler.TIMEOUT][0].callback
        self.start_cmd = self.start_handler.callback
        self.logout_cmd = self.logout_handler.callback
        self.help_cmd = self.help_handler.callback

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _private_chat_with_send(self) -> _FakeChat:
        return _FakeChat()


class PatOnlyAuthTests(AuthHandlerFixture):
    async def test_pat_is_accepted_and_stored(self) -> None:
        chat = self._private_chat_with_send()
        update, message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        stored = await self.store.get(1001)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.jira_pat, TEST_ONLY_PAT)
        self.jira.validate_credentials.assert_awaited_once_with(TEST_ONLY_PAT)
        message.delete.assert_awaited()
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)

    async def test_bearer_pat_is_accepted(self) -> None:
        chat = self._private_chat_with_send()
        update, _message = _update(text=f"Bearer {TEST_ONLY_PAT}")
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_awaited_once_with(TEST_ONLY_PAT)

    async def test_password_shape_is_rejected_without_validation(self) -> None:
        chat = self._private_chat_with_send()
        update, message = _update(text=TEST_ONLY_PASSWORD_SHAPE)
        update.effective_chat = chat
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        self.jira.validate_credentials.assert_not_awaited()
        self.assertIsNone(await self.store.get(1001))
        message.delete.assert_awaited()
        chat.send_message.assert_awaited()
        sent = chat.send_message.await_args.args[0]
        self.assertEqual(
            sent,
            user_message_for_denial(DenialCode.CREDENTIAL_FORMAT_REJECTED),
        )
        self.assertNotIn(TEST_ONLY_PASSWORD_SHAPE, sent)

    async def test_cookie_and_basic_shapes_are_rejected(self) -> None:
        for raw in (TEST_ONLY_COOKIE_SHAPE, TEST_ONLY_BASIC_SHAPE):
            with self.subTest(raw=raw):
                self.jira.validate_credentials.reset_mock()
                chat = self._private_chat_with_send()
                update, _message = _update(text=raw, user=_user(2002))
                update.effective_chat = chat
                ctx = _context(started_at=datetime.now(timezone.utc))

                result = await self.receive_pat(update, ctx)

                self.assertEqual(result, AWAITING_PAT)
                self.jira.validate_credentials.assert_not_awaited()

    async def test_auth_prompt_is_pat_only_copy(self) -> None:
        update, message = _update(text="/auth")
        ctx = _context()

        result = await self.auth_entry(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        message.reply_text.assert_awaited()
        prompt = message.reply_text.await_args.args[0]
        self.assertIn("PAT", prompt)
        self.assertNotIn("帳號密碼", prompt)
        self.assertNotIn("JSESSIONID", prompt)
        self.assertIn(AUTH_STARTED_AT_KEY, ctx.user_data)

    async def test_group_auth_entry_is_rejected(self) -> None:
        update, message = _update(text="/auth", chat=_group_chat())
        ctx = _context()

        result = await self.auth_entry(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)
        message.reply_text.assert_awaited()
        body = message.reply_text.await_args.args[0]
        self.assertIn("私聊", body)


class AuthTimeoutAndLateInputTests(AuthHandlerFixture):
    async def test_default_ttl_is_three_minutes(self) -> None:
        self.assertEqual(DEFAULT_AUTH_TTL_SECONDS, 180)
        self.assertEqual(AUTH_CONVERSATION_TTL, timedelta(minutes=3))
        self.assertEqual(self.auth_conv.conversation_timeout, float(DEFAULT_AUTH_TTL_SECONDS))

    async def test_expired_pat_input_is_not_validated(self) -> None:
        chat = self._private_chat_with_send()
        update, message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        started = datetime.now(timezone.utc) - timedelta(minutes=3, seconds=1)
        ctx = _context(started_at=started)

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_not_awaited()
        self.assertIsNone(await self.store.get(1001))
        message.delete.assert_awaited()
        chat.send_message.assert_awaited()
        sent = chat.send_message.await_args.args[0]
        self.assertEqual(sent, user_message_for_denial(DenialCode.AUTH_EXPIRED))
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)

    async def test_timeout_handler_ends_without_credential_use(self) -> None:
        update, message = _update(text="whatever")
        ctx = _context(started_at=datetime.now(timezone.utc) - timedelta(minutes=5))

        result = await self.auth_timeout(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_not_awaited()
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)
        message.reply_text.assert_awaited()
        self.assertEqual(
            message.reply_text.await_args.args[0],
            user_message_for_denial(DenialCode.AUTH_EXPIRED),
        )

    async def test_late_menu_input_is_not_treated_as_credentials(self) -> None:
        chat = self._private_chat_with_send()
        update, message = _update(text="📝 手動建立 Jira 工單")
        update.effective_chat = chat
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_not_awaited()
        message.delete.assert_not_awaited()
        message.reply_text.assert_awaited()
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)

    async def test_late_menu_after_expiry_uses_timeout_copy(self) -> None:
        update, message = _update(text="📖 說明")
        started = datetime.now(timezone.utc) - timedelta(minutes=4)
        ctx = _context(started_at=started)

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_not_awaited()
        self.assertEqual(
            message.reply_text.await_args.args[0],
            user_message_for_denial(DenialCode.AUTH_EXPIRED),
        )


class CredentialDeletionAndLoggingTests(AuthHandlerFixture):
    async def test_deletion_failure_warns_with_fixed_text(self) -> None:
        chat = self._private_chat_with_send()
        update, message = _update(text=TEST_ONLY_PASSWORD_SHAPE, delete_ok=False)
        update.effective_chat = chat
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        message.delete.assert_awaited()
        # First send_message is the delete-failure warning.
        warning = chat.send_message.await_args_list[0].args[0]
        self.assertEqual(
            warning,
            user_message_for_denial(DenialCode.CREDENTIAL_DELETE_FAILED),
        )
        self.assertNotIn(TEST_ONLY_PASSWORD_SHAPE, warning)
        self.assertNotIn(TEST_ONLY_PAT, warning)

    async def test_validation_failure_does_not_log_provider_or_pat(self) -> None:
        chat = self._private_chat_with_send()
        update, _message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        self.jira.validate_credentials = AsyncMock(
            side_effect=JiraClientError("secret provider body with token=XYZ")
        )
        ctx = _context(started_at=datetime.now(timezone.utc))

        with self.assertLogs("dztgbot.jira_auth", level="WARNING") as captured:
            result = await self.receive_pat(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        combined = "\n".join(captured.output)
        self.assertNotIn(TEST_ONLY_PAT, combined)
        self.assertNotIn("secret provider body", combined)
        self.assertNotIn("token=XYZ", combined)
        status.edit_text.assert_awaited()
        user_text = status.edit_text.await_args.args[0]
        self.assertNotIn("secret provider body", user_text)
        self.assertNotIn(TEST_ONLY_PAT, user_text)

    async def test_store_failure_is_failure_preserving(self) -> None:
        chat = self._private_chat_with_send()
        update, _message = _update(text=TEST_ONLY_PAT)
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        ctx = _context(started_at=datetime.now(timezone.utc))

        with patch.object(
            self.store, "store", AsyncMock(side_effect=OSError("disk full"))
        ):
            result = await self.receive_pat(update, ctx)

        self.assertEqual(result, AWAITING_PAT)
        self.assertIsNone(await self.store.get(1001))
        status.edit_text.assert_awaited()
        body = status.edit_text.await_args.args[0]
        self.assertIn("無法安全儲存", body)
        self.assertNotIn(TEST_ONLY_PAT, body)


class LogoutAndStartPrivacyTests(AuthHandlerFixture):
    async def test_logout_removes_local_only_and_does_not_claim_remote_revoke(
        self,
    ) -> None:
        self.assertFalse(logout_revokes_remote_pat())
        await self.store.store(
            1001,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(text="/logout")
        ctx = _context()

        await self.logout_cmd(update, ctx)

        self.assertIsNone(await self.store.get(1001))
        message.reply_text.assert_awaited()
        body = message.reply_text.await_args.args[0]
        self.assertIn("本機", body)
        self.assertIn("不會撤銷", body)
        self.assertNotIn("已撤銷 Jira", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_logout_in_group_does_not_reveal_auth_state(self) -> None:
        await self.store.store(
            1001,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(text="/logout", chat=_group_chat())
        ctx = _context()

        await self.logout_cmd(update, ctx)

        # Credentials remain; group must not learn auth state.
        self.assertIsNotNone(await self.store.get(1001))
        body = message.reply_text.await_args.args[0]
        self.assertIn("私聊", body)
        self.assertNotIn("Jira User", body)
        self.assertNotIn("jira.user", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_start_in_private_may_disclose_identity(self) -> None:
        await self.store.store(
            1001,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(text="/start")
        ctx = _context()

        await self.start_cmd(update, ctx)

        body = message.reply_text.await_args.args[0]
        self.assertIn("Jira User", body)
        self.assertIn("jira.user", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_start_in_group_does_not_disclose_identity(self) -> None:
        await self.store.store(
            1001,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(text="/start", chat=_group_chat())
        ctx = _context()

        await self.start_cmd(update, ctx)

        body = message.reply_text.await_args.args[0]
        self.assertIn("私聊", body)
        self.assertNotIn("Jira User", body)
        self.assertNotIn("jira.user", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_main_menu_keyboard_toggles(self) -> None:
        kbd = await get_main_menu_keyboard(1001, self.store)
        labels = [btn.text for row in kbd.keyboard for btn in row]
        self.assertIn("🔑 連結 Jira", labels)
        await self.store.store(
            1001,
            JiraCredentials("u", "U", TEST_ONLY_PAT),
        )
        kbd2 = await get_main_menu_keyboard(1001, self.store)
        labels2 = [btn.text for row in kbd2.keyboard for btn in row]
        self.assertIn("🚪 Logout", labels2)


class AllowedUserGateTests(unittest.IsolatedAsyncioTestCase):
    """Deterministic allowlist enforcement at credential-sensitive entry points."""

    ALLOWED_ID = 1001
    DENIED_ID = 9999

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "creds.json"
        self.store = UserStore(path)
        await self.store.initialize()
        self.jira = MagicMock()
        self.jira.validate_credentials = AsyncMock(
            return_value=JiraUser(
                username="jira.user",
                display_name="Jira User",
                email=None,
            )
        )
        self.allowlist = frozenset({self.ALLOWED_ID})
        (
            self.auth_conv,
            self.start_handler,
            self.logout_handler,
            self.help_handler,
        ) = build_auth_handlers(
            self.store,
            self.jira,
            "https://jira.test.example.com",
            auth_ttl_seconds=DEFAULT_AUTH_TTL_SECONDS,
            allowed_user_ids=self.allowlist,
        )
        self.auth_entry = self.auth_conv.entry_points[0].callback
        self.receive_pat = self.auth_conv.states[AWAITING_PAT][0].callback
        self.start_cmd = self.start_handler.callback
        self.logout_cmd = self.logout_handler.callback
        self.help_cmd = self.help_handler.callback
        self.not_allowed_copy = user_message_for_denial(DenialCode.NOT_ALLOWED_USER)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_privacy_safe_denial(self, body: str) -> None:
        self.assertEqual(body, self.not_allowed_copy)
        self.assertNotIn(TEST_ONLY_PAT, body)
        self.assertNotIn("Jira User", body)
        self.assertNotIn("jira.user", body)
        self.assertNotIn("綁定", body)
        self.assertNotIn("解綁", body)
        self.assertNotIn(str(self.ALLOWED_ID), body)
        self.assertNotIn(str(self.DENIED_ID), body)

    async def test_default_none_allowlist_is_unrestricted(self) -> None:
        """Omitting allowed_user_ids remains backward-compatible (unrestricted)."""

        (
            auth_conv,
            start_handler,
            logout_handler,
            help_handler,
        ) = build_auth_handlers(
            self.store,
            self.jira,
            "https://jira.test.example.com",
        )
        auth_entry = auth_conv.entry_points[0].callback
        update, message = _update(text="/auth", user=_user(self.DENIED_ID))
        result = await auth_entry(update, _context())
        self.assertEqual(result, AWAITING_PAT)
        prompt = message.reply_text.await_args.args[0]
        self.assertIn("PAT", prompt)

    async def test_allowed_user_auth_entry_succeeds(self) -> None:
        update, message = _update(text="/auth", user=_user(self.ALLOWED_ID))
        ctx = _context()
        result = await self.auth_entry(update, ctx)
        self.assertEqual(result, AWAITING_PAT)
        self.assertIn(AUTH_STARTED_AT_KEY, ctx.user_data)
        self.assertIn("PAT", message.reply_text.await_args.args[0])

    async def test_denied_user_auth_entry_is_rejected(self) -> None:
        update, message = _update(text="/auth", user=_user(self.DENIED_ID))
        ctx = _context()
        result = await self.auth_entry(update, ctx)
        self.assertEqual(result, ConversationHandler.END)
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)
        self._assert_privacy_safe_denial(message.reply_text.await_args.args[0])

    async def test_allowed_user_start_may_disclose_identity(self) -> None:
        await self.store.store(
            self.ALLOWED_ID,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(
            text="/start",
            user=_user(self.ALLOWED_ID),
            chat=_private_chat(self.ALLOWED_ID),
        )
        await self.start_cmd(update, _context())
        body = message.reply_text.await_args.args[0]
        self.assertIn("Jira User", body)
        self.assertIn("jira.user", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_denied_user_start_reveals_no_identity_or_auth_state(self) -> None:
        # Store under denied id would not normally occur; prove no disclosure path.
        await self.store.store(
            self.DENIED_ID,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(
            text="/start",
            user=_user(self.DENIED_ID),
            chat=_private_chat(self.DENIED_ID),
        )
        await self.start_cmd(update, _context())
        self._assert_privacy_safe_denial(message.reply_text.await_args.args[0])

    async def test_allowed_user_logout_removes_local_credentials(self) -> None:
        await self.store.store(
            self.ALLOWED_ID,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(
            text="/logout",
            user=_user(self.ALLOWED_ID),
            chat=_private_chat(self.ALLOWED_ID),
        )
        await self.logout_cmd(update, _context())
        self.assertIsNone(await self.store.get(self.ALLOWED_ID))
        body = message.reply_text.await_args.args[0]
        self.assertIn("本機", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_denied_user_logout_reveals_no_auth_state(self) -> None:
        await self.store.store(
            self.DENIED_ID,
            JiraCredentials(
                jira_username="jira.user",
                jira_display_name="Jira User",
                jira_pat=TEST_ONLY_PAT,
            ),
        )
        update, message = _update(
            text="/logout",
            user=_user(self.DENIED_ID),
            chat=_private_chat(self.DENIED_ID),
        )
        await self.logout_cmd(update, _context())
        # Store untouched; denial must not reveal binding state.
        self.assertIsNotNone(await self.store.get(self.DENIED_ID))
        self._assert_privacy_safe_denial(message.reply_text.await_args.args[0])

    async def test_allowed_user_help_is_served(self) -> None:
        update, message = _update(
            text="/help",
            user=_user(self.ALLOWED_ID),
            chat=_private_chat(self.ALLOWED_ID),
        )
        await self.help_cmd(update, _context())
        body = message.reply_text.await_args.args[0]
        self.assertIn("使用指南", body)
        self.assertNotIn(TEST_ONLY_PAT, body)

    async def test_denied_user_help_is_rejected(self) -> None:
        update, message = _update(
            text="/help",
            user=_user(self.DENIED_ID),
            chat=_private_chat(self.DENIED_ID),
        )
        await self.help_cmd(update, _context())
        self._assert_privacy_safe_denial(message.reply_text.await_args.args[0])

    async def test_allowed_user_receive_pat_succeeds(self) -> None:
        chat = _FakeChat(chat_id=self.ALLOWED_ID)
        update, message = _update(
            text=TEST_ONLY_PAT,
            user=_user(self.ALLOWED_ID),
            chat=_private_chat(self.ALLOWED_ID),
        )
        update.effective_chat = chat
        status = MagicMock()
        status.edit_text = AsyncMock()
        chat.send_message = AsyncMock(return_value=status)
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        stored = await self.store.get(self.ALLOWED_ID)
        self.assertIsNotNone(stored)
        self.jira.validate_credentials.assert_awaited_once_with(TEST_ONLY_PAT)
        message.delete.assert_awaited()

    async def test_denied_user_receive_pat_is_rejected_without_validation(self) -> None:
        chat = _FakeChat(chat_id=self.DENIED_ID)
        update, message = _update(
            text=TEST_ONLY_PAT,
            user=_user(self.DENIED_ID),
            chat=_private_chat(self.DENIED_ID),
        )
        update.effective_chat = chat
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_not_awaited()
        self.assertIsNone(await self.store.get(self.DENIED_ID))
        message.delete.assert_awaited()
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)
        self._assert_privacy_safe_denial(message.reply_text.await_args.args[0])

    async def test_denied_user_late_menu_input_is_rejected_without_delete(self) -> None:
        update, message = _update(
            text="📝 手動建立 Jira 工單",
            user=_user(self.DENIED_ID),
            chat=_private_chat(self.DENIED_ID),
        )
        ctx = _context(started_at=datetime.now(timezone.utc))

        result = await self.receive_pat(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.jira.validate_credentials.assert_not_awaited()
        message.delete.assert_not_awaited()
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx.user_data)
        self._assert_privacy_safe_denial(message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
