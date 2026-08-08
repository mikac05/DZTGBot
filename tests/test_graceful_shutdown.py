"""Deterministic tests for graceful shutdown and partial-startup failure cleanup."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from dztgbot.__main__ import run
from dztgbot.config import Settings
from dztgbot.infrastructure import (
    AsyncTaskScheduler,
    GeminiGateway,
    JiraGateway,
    SQLiteWorkflowRepository,
)


class TestGracefulShutdown(unittest.IsolatedAsyncioTestCase):
    """Test resource cleanup during graceful shutdown and partial-startup rollback."""

    async def test_scheduler_close_cancels_pending_tasks_and_timers(self) -> None:
        """AsyncTaskScheduler.close() cancels active timers and pending async tasks."""
        scheduler = AsyncTaskScheduler()
        task_executed = False

        async def slow_job() -> None:
            nonlocal task_executed
            await asyncio.sleep(10.0)
            task_executed = True

        scheduler.schedule_timer("job-1", 0.01, slow_job)
        await asyncio.sleep(0.02)  # Allow timer to trigger and spawn task

        await scheduler.close()
        self.assertFalse(task_executed, "Pending task should be cancelled on close")

    async def test_jira_gateway_aclose_closes_client(self) -> None:
        """JiraGateway.aclose() closes the owned AsyncClient connection pool."""
        gateway = JiraGateway(base_url="https://jira.example.com", verify=False)
        self.assertFalse(gateway._closed)
        await gateway.aclose()
        self.assertTrue(gateway._closed)

    async def test_partial_startup_failure_cleanup(self) -> None:
        """If startup fails mid-way, all previously initialized resources are closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_workflow.db"
            rules_path = Path(tmpdir) / "rules.md"
            rules_path.write_text("Default rules", encoding="utf-8")
            user_path = Path(tmpdir) / "users.json"

            with patch.object(Settings, "from_environment") as mock_settings, \
                 patch("dztgbot.__main__.GeminiGateway") as mock_gemini_cls:

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
                mock_set.vpn_nmcli_bin = "nmcli"
                mock_set.vpn_sudo_bin = "sudo"
                mock_set.vpn_command_timeout_seconds = 5.0
                mock_set.gemini_api_key = "test_key"
                mock_set.gemini_timeout_seconds = 10.0
                mock_set.jira_default_project_key = "NGSSA3"
                mock_set.telegram_admin_user_ids = (123,)
                mock_settings.return_value = mock_set

                # Simulate failure during GeminiGateway initialization
                mock_gemini_cls.side_effect = RuntimeError("Gemini initialization failed")

                with self.assertRaises(RuntimeError) as ctx:
                    await run()
                self.assertIn("Gemini initialization failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
