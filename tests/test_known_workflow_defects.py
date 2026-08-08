"""Regression gates for workflow defects resolved by the Phase 6 cutover."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram.ext import CallbackQueryHandler

from dztgbot.domain.callbacks import CallbackAction, parse_callback_data
from dztgbot.domain.policy import (
    CredentialInputKind,
    classify_credential_input,
    is_private_chat,
)
from dztgbot.services.callback_service import CallbackService
from dztgbot.services.intake_service import IntakeService
from dztgbot.services.workflow_service import WorkflowService
from dztgbot.ui.handlers import build_production_ui_handlers
from dztgbot.user_store import JiraCredentials, UserStore
from tests.support.security_fakes import (
    TEST_ONLY_COOKIE_SHAPE,
    TEST_ONLY_PASSWORD_SHAPE,
    TEST_ONLY_PAT,
)


LEGACY_WORKFLOW_KEYS = (
    "pending_template",
    "pending_photo_file_ids",
    "pending_batch",
    "editing_draft",
    "editing_published_key",
    "last_published",
)


def _source_tree() -> str:
    package = Path(__file__).parents[1] / "src" / "dztgbot"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package.rglob("*.py"))
    )


class CutoverInvariantTests(unittest.TestCase):
    def test_sqlite_cutover_removed_every_legacy_workflow_authority_key(self) -> None:
        source = _source_tree()
        for key in LEGACY_WORKFLOW_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, source)

    def test_application_paths_have_no_raw_asyncio_create_task(self) -> None:
        self.assertNotIn("asyncio.create_task", _source_tree())

    def test_production_callback_handler_accepts_only_bound_j1_data(self) -> None:
        handlers = build_production_ui_handlers(
            workflow_service=unittest.mock.MagicMock(spec=WorkflowService),
            intake_service=unittest.mock.MagicMock(spec=IntakeService),
            callback_service=unittest.mock.MagicMock(spec=CallbackService),
        )
        callback_handlers = [
            handler for handler in handlers if isinstance(handler, CallbackQueryHandler)
        ]
        self.assertEqual(len(callback_handlers), 1)
        pattern = callback_handlers[0].pattern
        self.assertIsNotNone(pattern)
        assert pattern is not None
        self.assertIsNotNone(pattern.match("j1:cfm:" + "a" * 32))
        self.assertIsNone(pattern.match("jira_confirm"))

    def test_bound_callback_grammar_round_trips(self) -> None:
        from dztgbot.domain.callbacks import encode_callback_data, generate_opaque_token

        encoded = encode_callback_data(CallbackAction.CONFIRM, generate_opaque_token())
        self.assertEqual(parse_callback_data(encoded).action, CallbackAction.CONFIRM)

    def test_auth_rejects_password_and_cookie_shapes(self) -> None:
        self.assertEqual(
            classify_credential_input(TEST_ONLY_PASSWORD_SHAPE),
            CredentialInputKind.REJECTED_PASSWORD,
        )
        self.assertEqual(
            classify_credential_input(TEST_ONLY_COOKIE_SHAPE),
            CredentialInputKind.REJECTED_COOKIE,
        )
        self.assertEqual(classify_credential_input(TEST_ONLY_PAT), CredentialInputKind.PAT)

    def test_private_chat_policy_remains_the_initial_scope(self) -> None:
        self.assertTrue(is_private_chat("private"))
        self.assertFalse(is_private_chat("group"))


class UserStoreMemoryDiskRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_write_failure_keeps_memory_and_disk_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            credentials = JiraCredentials(
                jira_username="user",
                jira_display_name="User",
                jira_pat=TEST_ONLY_PAT,
            )

            with patch.object(store, "_write_store", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    await store.store(7, credentials)

            self.assertIsNone(await store.get(7))
            if path.exists():
                self.assertNotIn(TEST_ONLY_PAT, path.read_text(encoding="utf-8"))

    def test_user_store_is_copy_on_write(self) -> None:
        source = inspect.getsource(UserStore.store)
        write_index = source.find("_write_store")
        assign_index = source.find("self._credentials =")
        self.assertGreater(write_index, 0)
        self.assertGreater(assign_index, write_index)


if __name__ == "__main__":
    unittest.main()
