"""Characterization tests for stateless compatibility helpers after cutover."""

from __future__ import annotations

import asyncio
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dztgbot.__main__ import handle_application_error
from dztgbot.core import MAX_BATCH_SIZE, MediaType, forwarded_message_in
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
        self.assertIs(forwarded_message_in(reply), reply.reply_to_message)  # type: ignore[arg-type]

    def test_ordinary_message_ignored(self) -> None:
        self.assertIsNone(forwarded_message_in(make_ordinary_message()))  # type: ignore[arg-type]

    def test_max_batch_size_is_twenty(self) -> None:
        self.assertEqual(MAX_BATCH_SIZE, 20)


class MediaCharacterization(unittest.TestCase):
    def test_forwarded_photo_message_carries_file_id(self) -> None:
        message = make_forwarded_photo_message(file_id="photo-abc")
        self.assertIsNotNone(message.photo)
        assert message.photo is not None
        self.assertEqual(message.photo[-1].file_id, "photo-abc")
        self.assertIsNotNone(message.forward_origin)


class PrivacyLoggingCharacterization(unittest.TestCase):
    def test_global_error_handler_logs_exception_type_only(self) -> None:
        context = FakeContext(error=RuntimeError("secret body with PAT=ATATT"))
        with patch("dztgbot.__main__.LOGGER") as logger:
            asyncio.run(
                handle_application_error(
                    object(), SimpleNamespace(error=context.error)  # type: ignore[arg-type]
                )
            )
            arguments = logger.error.call_args[0]
            rendered = arguments[0] % arguments[1:]
            self.assertIn("RuntimeError", rendered)
            self.assertNotIn("secret body", rendered)
            self.assertNotIn("ATATT", rendered)


class SourceSurfaceCharacterization(unittest.TestCase):
    def test_core_exports_only_stateless_forward_helpers(self) -> None:
        from dztgbot import core

        self.assertTrue(callable(core.forwarded_message_in))
        self.assertTrue(inspect.isfunction(core.extract_forwarded_message))
        self.assertEqual(core.MediaType.PHOTO, MediaType.PHOTO)

    def test_core_contains_no_legacy_callback_or_workflow_authority(self) -> None:
        import dztgbot.core as core

        source = Path(core.__file__).read_text(encoding="utf-8")
        for legacy in (
            "jira_confirm",
            "jira_editpublished",
            "pending_template",
            "pending_batch",
            "last_published",
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, source)


if __name__ == "__main__":
    unittest.main()
