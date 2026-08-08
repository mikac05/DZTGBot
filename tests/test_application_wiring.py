"""Deterministic tests for composition root wiring, DB authority, and handler setup."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler

from dztgbot.__main__ import KeyedUpdateProcessor, run
from dztgbot.config import Settings
from dztgbot.domain.callbacks import CallbackAction, CallbackTokenRecord
from dztgbot.infrastructure import (
    AsyncTaskScheduler,
    GeminiGateway,
    JiraGateway,
    SQLiteWorkflowRepository,
    SystemClock,
    UuidIdGenerator,
)
from dztgbot.infrastructure.keyed_processor import KeyedProcessor, WorkKey
from dztgbot.services import (
    AttachmentService,
    CallbackService,
    ConnectivityService,
    IntakeService,
    SubmissionService,
    WorkflowService,
)
from dztgbot.services.limits import ResourceKind, ResourceLimiter
from dztgbot.services.observability import SafeMetrics
from dztgbot.ui.handlers import build_production_ui_handlers


def _build_mock_settings(
    tmpdir: str,
    *,
    concurrent_updates: int = 1,
    max_concurrent_gemini: int = 2,
    max_concurrent_jira: int = 4,
    max_queue_size: int = 100,
    allowed_user_ids: frozenset[int] | None = None,
) -> MagicMock:
    db_path = Path(tmpdir) / "test_workflow.db"
    rules_path = Path(tmpdir) / "rules.md"
    rules_path.write_text("Default rules", encoding="utf-8")
    user_path = Path(tmpdir) / "users.json"

    mock_set = MagicMock()
    mock_set.log_level = "INFO"
    mock_set.workflow_db_path = str(db_path)
    mock_set.telegram_bot_token = "123456:TEST_TOKEN"
    mock_set.jira_url = "https://jira.example.com"
    mock_set.jira_verify_ssl = True
    mock_set.jira_timeout_seconds = 10.0
    mock_set.jira_rules_path = rules_path
    mock_set.user_credentials_path = user_path
    mock_set.vpn_enabled = False
    mock_set.vpn_connection_name = "vpn"
    mock_set.vpn_profile_path = Path(tmpdir) / "vpn.xml"
    mock_set.vpn_allow_start = False
    mock_set.vpn_nmcli_bin = Path("nmcli")
    mock_set.vpn_sudo_bin = Path("sudo")
    mock_set.vpn_command_timeout_seconds = 5.0
    mock_set.gemini_api_key = "test_key"
    mock_set.gemini_timeout_seconds = 10.0
    mock_set.jira_default_project_key = "NGSSA3"
    mock_set.telegram_admin_user_ids = (123,)
    mock_set.telegram_allowed_user_ids = allowed_user_ids
    mock_set.telegram_concurrent_updates = concurrent_updates
    mock_set.max_concurrent_gemini = max_concurrent_gemini
    mock_set.max_concurrent_jira = max_concurrent_jira
    mock_set.max_attachment_count = 10
    mock_set.max_queue_size = max_queue_size
    return mock_set


class TestApplicationWiring(unittest.IsolatedAsyncioTestCase):
    """Test composition root wiring, configuration requirements, and handler binding."""

    async def test_workflow_db_path_required(self) -> None:
        """Startup fails if WORKFLOW_DB_PATH is missing or empty."""
        with patch.object(Settings, "from_environment") as mock_settings:
            mock_set = MagicMock()
            mock_set.log_level = "INFO"
            mock_set.workflow_db_path = ""
            mock_settings.return_value = mock_set

            with self.assertRaises(RuntimeError) as ctx:
                await run()
            self.assertIn("WORKFLOW_DB_PATH", str(ctx.exception))

    async def test_concurrency_set_to_one(self) -> None:
        """Application is built with concurrent_updates=1 in Phase 6 fallback mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_set = _build_mock_settings(tmpdir, concurrent_updates=1)

            with patch.object(Settings, "from_environment", return_value=mock_set), \
                 patch("telegram.ext._applicationbuilder.ApplicationBuilder.build") as mock_build:

                app = MagicMock(spec=Application)
                app.bot_data = {}
                app.updater = MagicMock()
                app.updater.start_polling = AsyncMock()
                app.updater.stop = AsyncMock()
                app.start = AsyncMock()
                app.stop = AsyncMock()
                app.__aenter__ = AsyncMock(return_value=app)
                app.__aexit__ = AsyncMock()
                mock_build.return_value = app

                with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
                    try:
                        await run()
                    except asyncio.CancelledError:
                        pass

                self.assertIn("safe_metrics", app.bot_data)
                self.assertIn("resource_limiter", app.bot_data)
                self.assertNotIn("keyed_processor", app.bot_data)

    async def test_keyed_mode_selection(self) -> None:
        """When telegram_concurrent_updates > 1, KeyedProcessor and KeyedUpdateProcessor are instantiated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_set = _build_mock_settings(tmpdir, concurrent_updates=4, max_queue_size=50)

            with patch.object(Settings, "from_environment", return_value=mock_set), \
                 patch("telegram.ext._applicationbuilder.ApplicationBuilder.build") as mock_build:

                app = MagicMock(spec=Application)
                app.bot_data = {}
                app.updater = MagicMock()
                app.updater.start_polling = AsyncMock()
                app.updater.stop = AsyncMock()
                app.start = AsyncMock()
                app.stop = AsyncMock()
                app.__aenter__ = AsyncMock(return_value=app)
                app.__aexit__ = AsyncMock()
                mock_build.return_value = app

                with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
                    try:
                        await run()
                    except asyncio.CancelledError:
                        pass

                self.assertIn("keyed_processor", app.bot_data)
                processor = app.bot_data["keyed_processor"]
                self.assertIsInstance(processor, KeyedProcessor)
                self.assertEqual(processor._max_concurrency, 4)

    async def test_workflow_vs_collection_keys(self) -> None:
        """KeyedUpdateProcessor derives collection key for messages and workflow key for strict j1 callbacks."""
        processor = KeyedProcessor(max_concurrency=2, max_queue_size=10)
        repo = AsyncMock()
        adapter = KeyedUpdateProcessor(processor, repo)

        # Non-callback update (Message update from user 100, chat 200, thread 5)
        msg_update = MagicMock()
        msg_update.callback_query = None
        msg_update.effective_user.id = 100
        msg_update.effective_chat.id = 200
        msg_update.effective_message.message_thread_id = 5

        key1 = await adapter._derive_key(msg_update)
        self.assertEqual(key1.namespace, "collection")
        expected_coll = WorkKey.for_collection(actor_id=100, chat_id=200, message_thread_id=5)
        self.assertEqual(key1, expected_coll)

        # Strict j1 callback query with token resolving to SQLite record
        cb_update = MagicMock()
        opaque_token = "0123456789abcdef0123456789abcdef"
        cb_update.callback_query.data = f"j1:cfm:{opaque_token}"

        record = CallbackTokenRecord(
            token_hash="a" * 64,
            draft_id="draft-uuid-123",
            owner_user_id=100,
            chat_id=200,
            message_thread_id=None,
            preview_message_id=456,
            expected_revision=1,
            expected_state="DRAFT",
            action=CallbackAction.CONFIRM,
            expires_at=datetime.now(timezone.utc),
            one_shot=True,
        )
        repo.get_callback.return_value = record

        key2 = await adapter._derive_key(cb_update)
        self.assertEqual(key2.namespace, "workflow")
        expected_wf = WorkKey.for_workflow("draft-uuid-123")
        self.assertEqual(key2, expected_wf)

        # Malformed / unknown callback query falls back to collection key
        unknown_cb_update = MagicMock()
        unknown_cb_update.callback_query.data = "j1:cfm:99999999999999999999999999999999"
        unknown_cb_update.effective_user.id = 100
        unknown_cb_update.effective_chat.id = 200
        unknown_cb_update.effective_message.message_thread_id = None
        repo.get_callback.return_value = None

        key3 = await adapter._derive_key(unknown_cb_update)
        self.assertEqual(key3.namespace, "collection")
        self.assertEqual(key3, WorkKey.for_collection(actor_id=100, chat_id=200, message_thread_id=None))

        await processor.close()

    async def test_queue_bounds_and_settings_propagation(self) -> None:
        """ResourceLimiter and SafeMetrics inherit bounds from settings and propagate into bot_data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_set = _build_mock_settings(
                tmpdir,
                concurrent_updates=2,
                max_concurrent_gemini=3,
                max_concurrent_jira=6,
                max_queue_size=75,
            )
            with patch.object(Settings, "from_environment", return_value=mock_set), \
                 patch("telegram.ext._applicationbuilder.ApplicationBuilder.build") as mock_build:

                app = MagicMock(spec=Application)
                app.bot_data = {}
                app.updater = MagicMock()
                app.updater.start_polling = AsyncMock()
                app.updater.stop = AsyncMock()
                app.start = AsyncMock()
                app.stop = AsyncMock()
                app.__aenter__ = AsyncMock(return_value=app)
                app.__aexit__ = AsyncMock()
                mock_build.return_value = app

                with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
                    try:
                        await run()
                    except asyncio.CancelledError:
                        pass

                limiter: ResourceLimiter = app.bot_data["resource_limiter"]
                metrics: SafeMetrics = app.bot_data["safe_metrics"]
                self.assertIsInstance(limiter, ResourceLimiter)
                self.assertIsInstance(metrics, SafeMetrics)

                gemini_spec = limiter._states[ResourceKind.GEMINI].spec
                jira_spec = limiter._states[ResourceKind.JIRA].spec
                self.assertEqual(gemini_spec.global_limit, 3)
                self.assertEqual(gemini_spec.queue_limit, 75)
                self.assertEqual(jira_spec.global_limit, 6)
                self.assertEqual(jira_spec.queue_limit, 75)

    async def test_cleanup_on_normal_and_partial_startup(self) -> None:
        """Resources are closed deterministically on normal shutdown and partial startup failures."""
        # Normal shutdown cleanup check
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_set = _build_mock_settings(tmpdir, concurrent_updates=2)
            close_calls: list[str] = []

            async def mock_kp_close(self_obj: object) -> None:
                close_calls.append("keyed_processor")

            async def mock_rl_close(self_obj: object) -> None:
                close_calls.append("resource_limiter")

            async def mock_sched_close(self_obj: object) -> None:
                close_calls.append("scheduler")

            async def mock_repo_close(self_obj: object) -> None:
                close_calls.append("workflow_repo")

            with patch.object(Settings, "from_environment", return_value=mock_set), \
                 patch("telegram.ext._applicationbuilder.ApplicationBuilder.build") as mock_build, \
                 patch("dztgbot.__main__.KeyedProcessor.close", side_effect=mock_kp_close, autospec=True), \
                 patch("dztgbot.__main__.ResourceLimiter.close", side_effect=mock_rl_close, autospec=True), \
                 patch("dztgbot.infrastructure.AsyncTaskScheduler.close", side_effect=mock_sched_close, autospec=True), \
                 patch("dztgbot.infrastructure.SQLiteWorkflowRepository.close", side_effect=mock_repo_close, autospec=True):

                app = MagicMock(spec=Application)
                app.bot_data = {}
                app.updater = MagicMock()
                app.updater.start_polling = AsyncMock()
                app.updater.stop = AsyncMock()
                app.start = AsyncMock()
                app.stop = AsyncMock()
                app.__aenter__ = AsyncMock(return_value=app)
                app.__aexit__ = AsyncMock()
                mock_build.return_value = app

                with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
                    try:
                        await run()
                    except asyncio.CancelledError:
                        pass

                self.assertIn("keyed_processor", close_calls)
                self.assertIn("resource_limiter", close_calls)
                self.assertIn("scheduler", close_calls)
                self.assertIn("workflow_repo", close_calls)

        # Partial startup failure cleanup check
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_set = _build_mock_settings(tmpdir, concurrent_updates=2)
            partial_close_calls: list[str] = []

            async def mock_repo_close_p(self_obj: object) -> None:
                partial_close_calls.append("workflow_repo")

            with patch.object(Settings, "from_environment", return_value=mock_set), \
                 patch("dztgbot.__main__.JiraGateway", side_effect=RuntimeError("Jira init failure")), \
                 patch("dztgbot.infrastructure.SQLiteWorkflowRepository.close", side_effect=mock_repo_close_p, autospec=True):

                with self.assertRaises(RuntimeError) as ctx:
                    await run()
                self.assertIn("Jira init failure", str(ctx.exception))
                self.assertIn("workflow_repo", partial_close_calls)

    def test_production_ui_handlers_registration(self) -> None:
        """Production UI handlers register bound j1 callback and command handlers."""
        mock_ws = MagicMock(spec=WorkflowService)
        mock_intake = MagicMock(spec=IntakeService)
        mock_cb = MagicMock(spec=CallbackService)
        mock_sub = MagicMock(spec=SubmissionService)
        mock_att = MagicMock(spec=AttachmentService)

        handlers = build_production_ui_handlers(
            workflow_service=mock_ws,
            intake_service=mock_intake,
            callback_service=mock_cb,
            submission_service=mock_sub,
            attachment_service=mock_att,
        )

        self.assertGreater(len(handlers), 0)
        command_names = []
        has_callback = False

        for h in handlers:
            if isinstance(h, CommandHandler):
                command_names.extend(h.commands)
            elif isinstance(h, CallbackQueryHandler):
                has_callback = True
                self.assertIn("j1:", str(h.pattern))

        self.assertIn("new", command_names)
        self.assertIn("create", command_names)
        self.assertTrue(has_callback)

    def test_production_ui_handlers_allowlist_wiring(self) -> None:
        """Production UI handlers accept allowed_user_ids kwarg."""
        mock_ws = MagicMock(spec=WorkflowService)
        mock_intake = MagicMock(spec=IntakeService)
        mock_cb = MagicMock(spec=CallbackService)
        mock_sub = MagicMock(spec=SubmissionService)
        mock_att = MagicMock(spec=AttachmentService)
        allowlist = frozenset({1001, 1002})

        handlers = build_production_ui_handlers(
            workflow_service=mock_ws,
            intake_service=mock_intake,
            callback_service=mock_cb,
            submission_service=mock_sub,
            attachment_service=mock_att,
            allowed_user_ids=allowlist,
        )

        self.assertGreater(len(handlers), 0)

    async def test_run_passes_allowlist_to_auth_and_ui_handlers(self) -> None:
        """Run passes telegram_allowed_user_ids into build_auth_handlers and build_production_ui_handlers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_set = _build_mock_settings(tmpdir, allowed_user_ids=frozenset({1001}))

            with patch.object(Settings, "from_environment", return_value=mock_set), \
                 patch("dztgbot.__main__.build_auth_handlers") as mock_auth, \
                 patch("dztgbot.__main__.build_production_ui_handlers") as mock_ui, \
                 patch("telegram.ext._applicationbuilder.ApplicationBuilder.build") as mock_build:

                conv = MagicMock()
                h1, h2, h3 = MagicMock(), MagicMock(), MagicMock()
                mock_auth.return_value = (conv, h1, h2, h3)
                mock_ui.return_value = []

                app = MagicMock(spec=Application)
                app.bot_data = {}
                app.updater = MagicMock()
                app.updater.start_polling = AsyncMock()
                app.updater.stop = AsyncMock()
                app.start = AsyncMock()
                app.stop = AsyncMock()
                app.__aenter__ = AsyncMock(return_value=app)
                app.__aexit__ = AsyncMock()
                mock_build.return_value = app

                with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
                    try:
                        await run()
                    except asyncio.CancelledError:
                        pass

                mock_auth.assert_called_once()
                self.assertEqual(
                    mock_auth.call_args.kwargs.get("allowed_user_ids"),
                    frozenset({1001}),
                )
                mock_ui.assert_called_once()
                self.assertEqual(
                    mock_ui.call_args.kwargs.get("allowed_user_ids"),
                    frozenset({1001}),
                )


if __name__ == "__main__":
    unittest.main()

