"""Characterization tests for behavior that must survive refactoring (P0-C).

These assert current *desired-to-preserve* or *documented helper* behavior.
They must pass today and after cutover for pure helpers.
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dztgbot.core import (
    MAX_BATCH_SIZE,
    MediaType,
    forwarded_message_in,
)
from dztgbot.__main__ import handle_application_error
from tests.support.workflow_fakes import (
    FakeContext,
    make_forwarded_photo_message,
    make_forwarded_text_message,
    make_ordinary_message,
    make_reply_to_forward,
)


class ForwardIntakeCharacterization(unittest.TestCase):
    def test_direct_forward_selected(self) -> None:
        message = make_forwarded_text_message()
        self.assertIs(forwarded_message_in(message), message)  # type: ignore[arg-type]

    def test_reply_to_forward_selects_original(self) -> None:
        reply = make_reply_to_forward()
        selected = forwarded_message_in(reply)  # type: ignore[arg-type]
        self.assertIs(selected, reply.reply_to_message)

    def test_ordinary_message_ignored(self) -> None:
        ordinary = make_ordinary_message()
        self.assertIsNone(forwarded_message_in(ordinary))  # type: ignore[arg-type]

    def test_max_batch_size_is_twenty(self) -> None:
        self.assertEqual(MAX_BATCH_SIZE, 20)


class MediaCharacterization(unittest.TestCase):
    def test_forwarded_photo_message_carries_file_id(self) -> None:
        msg = make_forwarded_photo_message(file_id="photo-abc")
        self.assertIsNotNone(msg.photo)
        assert msg.photo is not None
        self.assertEqual(msg.photo[-1].file_id, "photo-abc")
        self.assertIsNotNone(msg.forward_origin)


class PrivacyLoggingCharacterization(unittest.TestCase):
    def test_global_error_handler_logs_exception_type_only(self) -> None:
        context = FakeContext(error=RuntimeError("secret body with PAT=ATATT"))
        # handle_application_error expects ContextTypes; use SimpleNamespace
        tg_context = SimpleNamespace(error=context.error)
        with patch("dztgbot.__main__.LOGGER") as logger:
            # unittest async: run coroutine
            import asyncio

            asyncio.run(handle_application_error(object(), tg_context))  # type: ignore[arg-type]
            logger.error.assert_called()
            args = logger.error.call_args[0]
            formatted = args[0] % args[1:] if len(args) > 1 else args[0]
            self.assertIn("RuntimeError", formatted)
            self.assertNotIn("secret body", formatted)
            self.assertNotIn("ATATT", formatted)


class SourceSurfaceCharacterization(unittest.TestCase):
    """Pin public symbols other modules and tests rely on."""

    def test_core_exports_forward_helpers(self) -> None:
        from dztgbot import core

        self.assertTrue(callable(core.forwarded_message_in))
        self.assertTrue(inspect.isfunction(core.extract_forwarded_message))
        self.assertEqual(core.MediaType.PHOTO, MediaType.PHOTO)

    def test_legacy_callback_action_strings_still_present_in_core_source(self) -> None:
        """Until Phase 5/6 cutover, production buttons use unbound action names."""
        import dztgbot.core as core_mod
        from pathlib import Path

        source = Path(core_mod.__file__).read_text(encoding="utf-8")
        for action in (
            "jira_confirm",
            "jira_edit",
            "jira_cancel",
            "jira_copylink",
            "jira_editpublished",
            "jira_toggle_type",
            "jira_toggle_priority",
        ):
            self.assertIn(action, source)


if __name__ == "__main__":
    unittest.main()
