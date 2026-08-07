"""Security policy tests for private-only, PAT-only, and callback authz (P1-G)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dztgbot.domain.callbacks import (
    CallbackAction,
    CallbackAuthorizationInput,
    build_token_record,
    generate_opaque_token,
)
from dztgbot.domain.policy import (
    AUTH_CONVERSATION_TTL,
    CredentialInputKind,
    DenialCode,
    auth_deadline,
    authorize_callback,
    classify_credential_input,
    credential_policy_decision,
    is_auth_expired,
    is_private_chat,
    logout_revokes_remote_pat,
    may_disclose_jira_identity,
    may_disclose_runtime_rules,
    normalize_pat_input,
    require_admin,
    require_allowed_user,
    require_private_admin,
    require_private_chat,
    user_message_for_denial,
)


def _utc(hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, 8, 7, hour, minute, tzinfo=timezone.utc)


class PrivateChatPolicyTests(unittest.TestCase):
    def test_private_allowed(self) -> None:
        self.assertTrue(is_private_chat("private"))
        self.assertTrue(require_private_chat("private").allowed)

    def test_groups_and_channels_denied(self) -> None:
        for chat_type in ("group", "supergroup", "channel", "GROUP"):
            with self.subTest(chat_type=chat_type):
                decision = require_private_chat(chat_type)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.denial_code, DenialCode.NOT_PRIVATE_CHAT)

    def test_none_denied(self) -> None:
        self.assertFalse(require_private_chat(None).allowed)

    def test_identity_and_rules_disclosure_private_only(self) -> None:
        self.assertTrue(may_disclose_jira_identity("private"))
        self.assertFalse(may_disclose_jira_identity("group"))
        self.assertTrue(may_disclose_runtime_rules("private"))
        self.assertFalse(may_disclose_runtime_rules("supergroup"))


class AdminAndAllowlistTests(unittest.TestCase):
    def test_admin_gate(self) -> None:
        admins = frozenset({1001, 1002})
        self.assertTrue(require_admin(1001, admins).allowed)
        denied = require_admin(9, admins)
        self.assertEqual(denied.denial_code, DenialCode.NOT_ADMIN)

    def test_private_admin_requires_both(self) -> None:
        admins = frozenset({1001})
        # Admin in group still denied.
        group = require_private_admin("group", 1001, admins)
        self.assertEqual(group.denial_code, DenialCode.NOT_PRIVATE_CHAT)
        # Non-admin in private denied as not admin.
        non_admin = require_private_admin("private", 9, admins)
        self.assertEqual(non_admin.denial_code, DenialCode.NOT_ADMIN)
        self.assertTrue(require_private_admin("private", 1001, admins).allowed)

    def test_allowlist_none_or_empty_means_open(self) -> None:
        self.assertTrue(require_allowed_user(1, None).allowed)
        self.assertTrue(require_allowed_user(1, frozenset()).allowed)

    def test_allowlist_enforced_when_configured(self) -> None:
        allowed = frozenset({5})
        self.assertTrue(require_allowed_user(5, allowed).allowed)
        denied = require_allowed_user(6, allowed)
        self.assertEqual(denied.denial_code, DenialCode.NOT_ALLOWED_USER)


class PatOnlyCredentialTests(unittest.TestCase):
    def test_raw_pat_accepted(self) -> None:
        self.assertEqual(
            classify_credential_input("ATATT3xFfGF0-example-pat-token-value"),
            CredentialInputKind.PAT,
        )
        self.assertTrue(credential_policy_decision("ATATT3xFfGF0-example-pat-token-value").allowed)
        self.assertEqual(
            normalize_pat_input("  ATATT3xFfGF0-example-pat-token-value  "),
            "ATATT3xFfGF0-example-pat-token-value",
        )

    def test_bearer_prefix_stripped(self) -> None:
        self.assertEqual(
            classify_credential_input("Bearer ATATT3xFfGF0-example-pat-token-value"),
            CredentialInputKind.PAT,
        )
        self.assertEqual(
            normalize_pat_input("Bearer ATATT3xFfGF0-example-pat-token-value"),
            "ATATT3xFfGF0-example-pat-token-value",
        )

    def test_password_shapes_rejected(self) -> None:
        cases = (
            "alice:s3cret",
            "user:pass:extra",
            "domain\\user:password",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(
                    classify_credential_input(raw),
                    CredentialInputKind.REJECTED_PASSWORD,
                )
                decision = credential_policy_decision(raw)
                self.assertEqual(
                    decision.denial_code, DenialCode.CREDENTIAL_FORMAT_REJECTED
                )
                self.assertIsNone(normalize_pat_input(raw))

    def test_basic_header_rejected(self) -> None:
        self.assertEqual(
            classify_credential_input("Basic dXNlcjpwYXNz"),
            CredentialInputKind.REJECTED_BASIC,
        )

    def test_session_cookie_rejected(self) -> None:
        for raw in (
            "JSESSIONID=ABC123",
            "jsessionid=abc",
            "Set-Cookie: JSESSIONID=xyz",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    classify_credential_input(raw),
                    CredentialInputKind.REJECTED_COOKIE,
                )
                self.assertIsNone(normalize_pat_input(raw))

    def test_empty_rejected(self) -> None:
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(
                    classify_credential_input(raw),
                    CredentialInputKind.REJECTED_EMPTY,
                )
                self.assertEqual(
                    credential_policy_decision(raw).denial_code,
                    DenialCode.CREDENTIAL_EMPTY,
                )

    def test_bearer_of_password_still_rejected(self) -> None:
        self.assertEqual(
            classify_credential_input("Bearer alice:s3cret"),
            CredentialInputKind.REJECTED_PASSWORD,
        )


class AuthTtlTests(unittest.TestCase):
    def test_ttl_is_three_minutes(self) -> None:
        self.assertEqual(AUTH_CONVERSATION_TTL, timedelta(minutes=3))

    def test_deadline_and_expiry(self) -> None:
        started = _utc(12, 0)
        self.assertEqual(auth_deadline(started), _utc(12, 3))
        self.assertFalse(is_auth_expired(started, _utc(12, 2)))
        self.assertTrue(is_auth_expired(started, _utc(12, 3)))
        self.assertTrue(is_auth_expired(started, _utc(12, 4)))


class CallbackAuthorizationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = generate_opaque_token()
        self.now = _utc(12, 0)
        self.expires = self.now + timedelta(hours=1)
        self.record = build_token_record(
            opaque_token=self.token,
            draft_id="draft-abc",
            owner_user_id=42,
            chat_id=100,
            action=CallbackAction.CONFIRM,
            expected_revision=2,
            expected_state="review",
            expires_at=self.expires,
            preview_message_id=500,
            message_thread_id=None,
        )

    def _request(self, **overrides: object) -> CallbackAuthorizationInput:
        values: dict[str, object] = {
            "actor_user_id": 42,
            "chat_id": 100,
            "chat_type": "private",
            "message_thread_id": None,
            "preview_message_id": 500,
            "action": CallbackAction.CONFIRM,
            "opaque_token": self.token,
            "now": self.now,
        }
        values.update(overrides)
        return CallbackAuthorizationInput(**values)  # type: ignore[arg-type]

    def test_happy_path_allows(self) -> None:
        decision = authorize_callback(
            self._request(),
            self.record,
            current_revision=2,
            current_state="review",
        )
        self.assertTrue(decision.allowed)

    def test_group_chat_denied_even_with_valid_token(self) -> None:
        decision = authorize_callback(
            self._request(chat_type="group"),
            self.record,
            current_revision=2,
            current_state="review",
        )
        self.assertEqual(decision.denial_code, DenialCode.NOT_PRIVATE_CHAT)

    def test_unknown_record(self) -> None:
        decision = authorize_callback(self._request(), None)
        self.assertEqual(decision.denial_code, DenialCode.UNKNOWN_TOKEN)

    def test_wrong_token_material(self) -> None:
        other = generate_opaque_token()
        decision = authorize_callback(self._request(opaque_token=other), self.record)
        self.assertEqual(decision.denial_code, DenialCode.UNKNOWN_TOKEN)

    def test_foreign_actor(self) -> None:
        decision = authorize_callback(self._request(actor_user_id=99), self.record)
        self.assertEqual(decision.denial_code, DenialCode.FOREIGN_ACTOR)

    def test_wrong_chat(self) -> None:
        decision = authorize_callback(self._request(chat_id=999), self.record)
        self.assertEqual(decision.denial_code, DenialCode.WRONG_CHAT)

    def test_wrong_preview_message(self) -> None:
        decision = authorize_callback(
            self._request(preview_message_id=1),
            self.record,
            current_revision=2,
            current_state="review",
        )
        self.assertEqual(decision.denial_code, DenialCode.WRONG_MESSAGE)

    def test_wrong_thread_when_bound(self) -> None:
        record = build_token_record(
            opaque_token=self.token,
            draft_id="draft-abc",
            owner_user_id=42,
            chat_id=100,
            action=CallbackAction.CONFIRM,
            expected_revision=2,
            expected_state="review",
            expires_at=self.expires,
            message_thread_id=7,
            preview_message_id=500,
        )
        decision = authorize_callback(
            self._request(message_thread_id=8),
            record,
            current_revision=2,
            current_state="review",
        )
        self.assertEqual(decision.denial_code, DenialCode.WRONG_THREAD)

    def test_action_mismatch(self) -> None:
        decision = authorize_callback(
            self._request(action=CallbackAction.CANCEL),
            self.record,
        )
        self.assertEqual(decision.denial_code, DenialCode.ACTION_MISMATCH)

    def test_expired_token(self) -> None:
        decision = authorize_callback(
            self._request(now=self.expires),
            self.record,
            current_revision=2,
            current_state="review",
        )
        self.assertEqual(decision.denial_code, DenialCode.TOKEN_EXPIRED)

    def test_consumed_token(self) -> None:
        consumed = build_token_record(
            opaque_token=self.token,
            draft_id="draft-abc",
            owner_user_id=42,
            chat_id=100,
            action=CallbackAction.CONFIRM,
            expected_revision=2,
            expected_state="review",
            expires_at=self.expires,
            preview_message_id=500,
        )
        # frozen dataclass — rebuild with consumed_at via object pattern
        from dztgbot.domain.callbacks import CallbackTokenRecord

        consumed_record = CallbackTokenRecord(
            token_hash=consumed.token_hash,
            draft_id=consumed.draft_id,
            owner_user_id=consumed.owner_user_id,
            chat_id=consumed.chat_id,
            message_thread_id=consumed.message_thread_id,
            preview_message_id=consumed.preview_message_id,
            expected_revision=consumed.expected_revision,
            expected_state=consumed.expected_state,
            action=consumed.action,
            expires_at=consumed.expires_at,
            one_shot=consumed.one_shot,
            consumed_at=self.now,
        )
        decision = authorize_callback(
            self._request(),
            consumed_record,
            current_revision=2,
            current_state="review",
        )
        self.assertEqual(decision.denial_code, DenialCode.TOKEN_CONSUMED)

    def test_stale_revision(self) -> None:
        decision = authorize_callback(
            self._request(),
            self.record,
            current_revision=9,
            current_state="review",
        )
        self.assertEqual(decision.denial_code, DenialCode.STALE_REVISION)

    def test_illegal_state_and_already_processing(self) -> None:
        illegal = authorize_callback(
            self._request(),
            self.record,
            current_revision=2,
            current_state="cancelled",
        )
        self.assertEqual(illegal.denial_code, DenialCode.ILLEGAL_STATE)

        processing = authorize_callback(
            self._request(),
            self.record,
            current_revision=2,
            current_state="submitting",
        )
        self.assertEqual(processing.denial_code, DenialCode.ALREADY_PROCESSING)

    def test_token_possession_alone_insufficient_without_actor_match(self) -> None:
        """Same chat and valid token but different actor must fail closed."""

        decision = authorize_callback(
            self._request(actor_user_id=777),
            self.record,
            current_revision=2,
            current_state="review",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denial_code, DenialCode.FOREIGN_ACTOR)


class DenialMessageSafetyTests(unittest.TestCase):
    def test_every_code_has_fixed_message(self) -> None:
        for code in DenialCode:
            message = user_message_for_denial(code)
            self.assertIsInstance(message, str)
            self.assertTrue(message)
            # Must not look like it embeds raw secrets/callback data markers.
            self.assertNotIn("j1:", message)
            self.assertNotIn("JSESSIONID", message)

    def test_logout_does_not_claim_remote_revoke(self) -> None:
        self.assertFalse(logout_revokes_remote_pat())


if __name__ == "__main__":
    unittest.main()
