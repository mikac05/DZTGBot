"""Security contract baseline tests (P0-G).

Domain policy contracts that first-release must enforce. Runtime handlers may
not all be wired yet; these tests lock the *policy* layer and config defaults.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dztgbot.config import Settings
from dztgbot.domain.callbacks import (
    CallbackAction,
    CallbackAuthorizationInput,
    build_token_record,
    generate_opaque_token,
    parse_callback_data,
)
from dztgbot.domain.policy import (
    AUTH_CONVERSATION_TTL,
    DenialCode,
    authorize_callback,
    classify_credential_input,
    CredentialInputKind,
    is_auth_expired,
    may_disclose_jira_identity,
    may_disclose_runtime_rules,
    require_private_admin,
    require_private_chat,
)
from tests.support.security_fakes import (
    TEST_ONLY_BASIC_SHAPE,
    TEST_ONLY_COOKIE_SHAPE,
    TEST_ONLY_PASSWORD_SHAPE,
    TEST_ONLY_PAT,
    minimal_env,
)


class PrivateBoundaryContracts(unittest.TestCase):
    def test_workflows_require_private_chat(self) -> None:
        self.assertTrue(require_private_chat("private").allowed)
        for chat_type in ("group", "supergroup", "channel"):
            decision = require_private_chat(chat_type)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.denial_code, DenialCode.NOT_PRIVATE_CHAT)

    def test_admin_requires_private_and_admin_id(self) -> None:
        admins = frozenset({1001})
        self.assertTrue(require_private_admin("private", 1001, admins).allowed)
        self.assertEqual(
            require_private_admin("group", 1001, admins).denial_code,
            DenialCode.NOT_PRIVATE_CHAT,
        )
        self.assertEqual(
            require_private_admin("private", 9, admins).denial_code,
            DenialCode.NOT_ADMIN,
        )

    def test_group_must_not_disclose_identity_or_rules(self) -> None:
        self.assertFalse(may_disclose_jira_identity("group"))
        self.assertFalse(may_disclose_runtime_rules("supergroup"))
        self.assertTrue(may_disclose_jira_identity("private"))
        self.assertTrue(may_disclose_runtime_rules("private"))


class PatOnlyContracts(unittest.TestCase):
    def test_pat_accepted_password_cookie_basic_rejected(self) -> None:
        table = (
            (TEST_ONLY_PAT, CredentialInputKind.PAT),
            (f"Bearer {TEST_ONLY_PAT}", CredentialInputKind.PAT),
            (TEST_ONLY_PASSWORD_SHAPE, CredentialInputKind.REJECTED_PASSWORD),
            (TEST_ONLY_COOKIE_SHAPE, CredentialInputKind.REJECTED_COOKIE),
            (TEST_ONLY_BASIC_SHAPE, CredentialInputKind.REJECTED_BASIC),
            ("", CredentialInputKind.REJECTED_EMPTY),
        )
        for raw, expected in table:
            with self.subTest(raw=raw):
                self.assertEqual(classify_credential_input(raw or None), expected)


class AuthExpiryContracts(unittest.TestCase):
    def test_auth_ttl_is_three_minutes(self) -> None:
        self.assertEqual(AUTH_CONVERSATION_TTL, timedelta(minutes=3))
        started = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(is_auth_expired(started, started + timedelta(minutes=2, seconds=59)))
        self.assertTrue(is_auth_expired(started, started + timedelta(minutes=3)))


class CallbackSecurityContracts(unittest.TestCase):
    def test_stale_and_foreign_callbacks_denied(self) -> None:
        token = generate_opaque_token()
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        record = build_token_record(
            opaque_token=token,
            draft_id="d1",
            owner_user_id=10,
            chat_id=10,
            action=CallbackAction.CONFIRM,
            expected_revision=1,
            expected_state="review",
            expires_at=now + timedelta(hours=1),
            preview_message_id=99,
        )
        foreign = CallbackAuthorizationInput(
            actor_user_id=99,
            chat_id=10,
            chat_type="private",
            message_thread_id=None,
            preview_message_id=99,
            action=CallbackAction.CONFIRM,
            opaque_token=token,
            now=now,
        )
        self.assertEqual(
            authorize_callback(foreign, record, current_revision=1, current_state="review").denial_code,
            DenialCode.FOREIGN_ACTOR,
        )
        stale = CallbackAuthorizationInput(
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            message_thread_id=None,
            preview_message_id=99,
            action=CallbackAction.CONFIRM,
            opaque_token=token,
            now=now,
        )
        self.assertEqual(
            authorize_callback(stale, record, current_revision=2, current_state="review").denial_code,
            DenialCode.STALE_REVISION,
        )

    def test_legacy_unbound_callback_rejected_by_grammar(self) -> None:
        with self.assertRaises(Exception):
            parse_callback_data("jira_confirm")


class TlsConfigContracts(unittest.TestCase):
    def test_verify_ssl_defaults_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = minimal_env(rules_path=str(rules))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertTrue(settings.jira_verify_ssl)

    def test_verify_ssl_can_be_disabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = minimal_env(rules_path=str(rules), verify_ssl="false")
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertFalse(settings.jira_verify_ssl)


class CredentialDeletionFailureContract(unittest.TestCase):
    def test_policy_exposes_delete_failure_denial_code(self) -> None:
        """Handlers must warn users; policy provides a fixed non-leaky code."""
        self.assertEqual(
            DenialCode.CREDENTIAL_DELETE_FAILED.value,
            "credential_delete_failed",
        )
        from dztgbot.domain.policy import user_message_for_denial

        message = user_message_for_denial(DenialCode.CREDENTIAL_DELETE_FAILED)
        self.assertIn("手動刪除", message)
        self.assertNotIn(TEST_ONLY_PAT, message)


if __name__ == "__main__":
    unittest.main()
