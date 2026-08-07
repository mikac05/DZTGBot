"""Exhaustive grammar and token tests for domain.callbacks (P1-G)."""

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from dztgbot.domain.callbacks import (
    CALLBACK_DATA_MAX_LENGTH,
    CALLBACK_VERSION,
    OPAQUE_TOKEN_BYTES,
    OPAQUE_TOKEN_HEX_LENGTH,
    CallbackAction,
    CallbackAuthorizationInput,
    CallbackParseError,
    CallbackTokenRecord,
    ONE_SHOT_ACTIONS,
    build_token_record,
    default_one_shot,
    encode_callback_data,
    generate_opaque_token,
    hash_opaque_token,
    parse_callback_data,
)


class OpaqueTokenTests(unittest.TestCase):
    def test_generated_token_is_at_least_128_bits_hex(self) -> None:
        token = generate_opaque_token()
        self.assertEqual(len(token), OPAQUE_TOKEN_HEX_LENGTH)
        self.assertRegex(token, r"\A[0-9a-f]+\Z")
        # 32 hex chars = 16 bytes = 128 bits
        self.assertGreaterEqual(len(bytes.fromhex(token)), OPAQUE_TOKEN_BYTES)

    def test_rejects_sub_128_bit_generation(self) -> None:
        with self.assertRaises(ValueError):
            generate_opaque_token(nbytes=15)

    def test_tokens_are_unique(self) -> None:
        tokens = {generate_opaque_token() for _ in range(64)}
        self.assertEqual(len(tokens), 64)

    def test_hash_is_sha256_hex_and_stable(self) -> None:
        token = "a" * OPAQUE_TOKEN_HEX_LENGTH
        digest = hash_opaque_token(token)
        self.assertEqual(len(digest), 64)
        self.assertEqual(
            digest,
            hashlib.sha256(token.encode("ascii")).hexdigest(),
        )
        self.assertEqual(hash_opaque_token(token), digest)

    def test_hash_rejects_non_canonical_token_without_echo(self) -> None:
        with self.assertRaises(CallbackParseError) as ctx:
            hash_opaque_token("NOT-A-TOKEN")
        self.assertEqual(ctx.exception.code, "callback_token_alphabet")
        self.assertNotIn("NOT-A-TOKEN", str(ctx.exception))


class CallbackGrammarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = generate_opaque_token()

    def test_round_trip_for_every_allowlisted_action(self) -> None:
        for action in CallbackAction:
            with self.subTest(action=action.value):
                encoded = encode_callback_data(action, self.token)
                self.assertLessEqual(len(encoded), CALLBACK_DATA_MAX_LENGTH)
                parsed = parse_callback_data(encoded)
                self.assertEqual(parsed.version, CALLBACK_VERSION)
                self.assertEqual(parsed.action, action)
                self.assertEqual(parsed.opaque_token, self.token)
                self.assertEqual(parsed.encode(), encoded)

    def test_example_wire_format_shape(self) -> None:
        encoded = encode_callback_data(CallbackAction.CONFIRM, self.token)
        self.assertTrue(encoded.startswith("j1:cfm:"))
        self.assertEqual(encoded, f"j1:cfm:{self.token}")

    def test_string_action_must_be_allowlisted(self) -> None:
        with self.assertRaises(CallbackParseError) as ctx:
            encode_callback_data("nope", self.token)
        self.assertEqual(ctx.exception.code, "callback_action_unknown")

    def _assert_parse_code(self, raw: str | None, code: str) -> None:
        with self.assertRaises(CallbackParseError) as ctx:
            parse_callback_data(raw)
        self.assertEqual(ctx.exception.code, code)
        # Never echo attacker payload.
        if isinstance(raw, str) and raw:
            self.assertNotIn(raw, str(ctx.exception))

    def test_missing_and_empty(self) -> None:
        self._assert_parse_code(None, "callback_data_missing")
        self._assert_parse_code("", "callback_data_empty")

    def test_too_long(self) -> None:
        raw = "x" * (CALLBACK_DATA_MAX_LENGTH + 1)
        self._assert_parse_code(raw, "callback_data_too_long")

    def test_non_ascii_rejected(self) -> None:
        self._assert_parse_code("j1:cfm:你好" + "a" * 20, "callback_data_non_ascii")

    def test_unsupported_version(self) -> None:
        raw = f"j2:cfm:{self.token}"
        self._assert_parse_code(raw, "callback_version_unsupported")

    def test_wrong_segment_count(self) -> None:
        self._assert_parse_code(f"j1:cfm:{self.token}:extra", "callback_segment_count")
        self._assert_parse_code("j1:cfm", "callback_segment_count")

    def test_unknown_action(self) -> None:
        raw = f"j1:zzzz:{self.token}"
        self._assert_parse_code(raw, "callback_action_unknown")

    def test_action_alphabet(self) -> None:
        raw = f"j1:CFM:{self.token}"  # uppercase not allowed
        self._assert_parse_code(raw, "callback_action_alphabet")

    def test_token_must_be_exact_hex_length(self) -> None:
        short = "ab" * 8  # 16 hex chars = 64 bits only
        self._assert_parse_code(f"j1:cfm:{short}", "callback_token_alphabet")
        # Longer-than-32 hex still matches segment structure but fails alphabet/length.
        long_token = "ab" * 20  # 40 hex chars
        self._assert_parse_code(f"j1:cfm:{long_token}", "callback_token_alphabet")

    def test_token_rejects_uppercase_hex(self) -> None:
        upper = self.token.upper()
        self._assert_parse_code(f"j1:cfm:{upper}", "callback_token_alphabet")

    def test_legacy_unbound_callbacks_rejected(self) -> None:
        for legacy in (
            "jira_confirm",
            "jira_edit",
            "jira_cancel",
            "jira_copylink",
            "jira_toggle_type",
        ):
            with self.subTest(legacy=legacy):
                self._assert_parse_code(legacy, "callback_version_unsupported")

    def test_encoded_confirm_fits_telegram_limit(self) -> None:
        encoded = encode_callback_data(CallbackAction.CONFIRM, generate_opaque_token())
        self.assertEqual(len(encoded), len("j1:cfm:") + OPAQUE_TOKEN_HEX_LENGTH)
        self.assertLessEqual(len(encoded), 64)


class TokenRecordTests(unittest.TestCase):
    def test_build_token_record_hashes_and_defaults_one_shot(self) -> None:
        token = generate_opaque_token()
        expires = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        record = build_token_record(
            opaque_token=token,
            draft_id="draft-1",
            owner_user_id=42,
            chat_id=99,
            action=CallbackAction.CONFIRM,
            expected_revision=3,
            expected_state="review",
            expires_at=expires,
            preview_message_id=1001,
        )
        self.assertEqual(record.token_hash, hash_opaque_token(token))
        self.assertTrue(record.one_shot)
        self.assertIsNone(record.consumed_at)
        self.assertEqual(record.action, CallbackAction.CONFIRM)
        self.assertEqual(record.preview_message_id, 1001)

    def test_toggle_actions_are_not_one_shot_by_default(self) -> None:
        self.assertFalse(default_one_shot(CallbackAction.TOGGLE_TYPE))
        self.assertFalse(default_one_shot(CallbackAction.TOGGLE_PRIORITY))
        self.assertTrue(CallbackAction.CONFIRM in ONE_SHOT_ACTIONS)

    def test_record_rejects_naive_expiry(self) -> None:
        token = generate_opaque_token()
        with self.assertRaises(ValueError):
            CallbackTokenRecord(
                token_hash=hash_opaque_token(token),
                draft_id="d1",
                owner_user_id=1,
                chat_id=2,
                message_thread_id=None,
                preview_message_id=None,
                expected_revision=1,
                expected_state="review",
                action=CallbackAction.CONFIRM,
                expires_at=datetime(2026, 1, 1),  # naive
                one_shot=True,
            )

    def test_authorization_input_rejects_bad_token_shape(self) -> None:
        with self.assertRaises(ValueError):
            CallbackAuthorizationInput(
                actor_user_id=1,
                chat_id=2,
                chat_type="private",
                message_thread_id=None,
                preview_message_id=10,
                action=CallbackAction.CONFIRM,
                opaque_token="short",
                now=datetime.now(timezone.utc),
            )


class CallbackActionAllowlistTests(unittest.TestCase):
    def test_allowlist_is_closed(self) -> None:
        allowed = CallbackAction.allowlist()
        self.assertIn("cfm", allowed)
        self.assertIn("ttyp", allowed)
        self.assertNotIn("confirm", allowed)
        self.assertNotIn("jira_confirm", allowed)


if __name__ == "__main__":
    unittest.main()
