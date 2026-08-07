"""Privacy logging contracts (P0-G).

Logs and error surfaces must not contain message text, tokens, callback
payloads, or provider error bodies.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dztgbot.__main__ import handle_application_error
from dztgbot.domain.callbacks import CallbackParseError, parse_callback_data
from dztgbot.domain.errors import (
    SafeErrorCode,
    classify_unknown_mutation_outcome,
    Operation,
    ErrorKind,
)
from dztgbot.domain.policy import DenialCode, user_message_for_denial
from tests.support.security_fakes import TEST_ONLY_PAT


class GlobalErrorHandlerPrivacy(unittest.TestCase):
    def test_does_not_serialize_exception_message_or_update(self) -> None:
        secret = f"leaked {TEST_ONLY_PAT} and forwarded text BODY"
        context = SimpleNamespace(error=ValueError(secret))
        with patch("dztgbot.__main__.LOGGER") as logger:
            asyncio.run(handle_application_error({"text": secret}, context))  # type: ignore[arg-type]
            logger.error.assert_called_once()
            call_args = logger.error.call_args[0]
            rendered = call_args[0] % call_args[1:] if len(call_args) > 1 else call_args[0]
            self.assertIn("ValueError", rendered)
            self.assertNotIn(TEST_ONLY_PAT, rendered)
            self.assertNotIn("forwarded text", rendered)
            self.assertNotIn(secret, rendered)


class DomainErrorPrivacy(unittest.TestCase):
    def test_classified_errors_expose_safe_code_only(self) -> None:
        classification = classify_unknown_mutation_outcome(
            operation=Operation.JIRA_CREATE,
            kind=ErrorKind.TIMEOUT,
        )
        self.assertEqual(classification.safe_code, SafeErrorCode.OUTCOME_UNKNOWN)
        self.assertEqual(str(classification.safe_code.value), "outcome_unknown")

    def test_callback_parse_errors_do_not_echo_raw_payload(self) -> None:
        evil = "j1:cfm:" + "deadbeef" * 4 + ":EXTRA_SECRET_PAYLOAD"
        try:
            parse_callback_data(evil)
            self.fail("expected CallbackParseError")
        except CallbackParseError as error:
            self.assertNotIn("EXTRA_SECRET", str(error))
            self.assertNotIn(evil, str(error))
            self.assertTrue(error.code.startswith("callback_"))


class DenialMessagePrivacy(unittest.TestCase):
    def test_denial_messages_never_include_pat_or_callback_wire_format(self) -> None:
        for code in DenialCode:
            message = user_message_for_denial(code)
            self.assertNotIn(TEST_ONLY_PAT, message)
            self.assertNotIn("j1:", message)
            self.assertNotIn("Bearer ", message)


if __name__ == "__main__":
    unittest.main()
