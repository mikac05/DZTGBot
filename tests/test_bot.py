from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dztgbot.__main__ import handle_application_error
from dztgbot.admin import _rules_from_command, _telegram_chunks
from dztgbot.analysis import GeminiAnalyzer, JiraTaskTemplate, jira_template_preview
from dztgbot.config import Settings
from dztgbot.core import ForwardedMessage, MediaType, TelegramIdentity, forwarded_message_in
from dztgbot.rules import RulesStore
from dztgbot.user_store import JiraCredentials, UserStore
from dztgbot.vpn import NetworkManagerL2tpManager, VpnState


class SettingsTests(unittest.TestCase):
    def test_required_placeholder_is_rejected(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "TODO_REPLACE_WITH_TELEGRAM_BOT_TOKEN",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
                Settings.from_environment()

    def test_disabled_vpn_does_not_require_private_profile(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "TEST_ONLY_NOT_A_REAL_TELEGRAM_TOKEN",
            "GEMINI_API_KEY": "TEST_ONLY_NOT_A_REAL_GEMINI_KEY",
            "GEMINI_MODEL": "TEST_ONLY_MODEL_IDENTIFIER",
            "TELEGRAM_ADMIN_USER_IDS": "1001,1002",
            "JIRA_RULES_PATH": "var/test-rules.txt",
            "JIRA_URL": "https://jira.test.example.com",
            "VPN_ENABLED": "false",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()
        self.assertFalse(settings.vpn_enabled)
        self.assertEqual(settings.telegram_admin_user_ids, frozenset({1001, 1002}))
        self.assertEqual(settings.telegram_concurrent_updates, 4)
        self.assertEqual(settings.jira_url, "https://jira.test.example.com")
        self.assertEqual(
            settings.user_credentials_path,
            Path("var") / "user_credentials.json",
        )

    def test_remote_vpn_start_requires_vpn_to_be_enabled(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "TEST_ONLY_NOT_A_REAL_TELEGRAM_TOKEN",
            "GEMINI_API_KEY": "TEST_ONLY_NOT_A_REAL_GEMINI_KEY",
            "GEMINI_MODEL": "TEST_ONLY_MODEL_IDENTIFIER",
            "TELEGRAM_ADMIN_USER_IDS": "1001",
            "JIRA_RULES_PATH": "var/test-rules.txt",
            "JIRA_URL": "https://jira.test.example.com",
            "VPN_ENABLED": "false",
            "VPN_ALLOW_START": "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "VPN_ALLOW_START"):
                Settings.from_environment()


class ForwardTests(unittest.TestCase):
    def test_direct_forward_is_selected(self) -> None:
        message = SimpleNamespace(forward_origin=object(), reply_to_message=None)
        self.assertIs(forwarded_message_in(message), message)

    def test_reply_to_forward_selects_original_forward(self) -> None:
        forwarded = SimpleNamespace(forward_origin=object())
        reply = SimpleNamespace(forward_origin=None, reply_to_message=forwarded)
        self.assertIs(forwarded_message_in(reply), forwarded)

    def test_ordinary_message_is_ignored(self) -> None:
        message = SimpleNamespace(forward_origin=None, reply_to_message=None)
        self.assertIsNone(forwarded_message_in(message))


class AdminCommandTests(unittest.TestCase):
    def test_inline_rules_take_precedence(self) -> None:
        replied = SimpleNamespace(text="replied rules", caption=None)
        self.assertEqual(_rules_from_command("/setrules inline rules", replied), "inline rules")

    def test_replied_text_is_accepted(self) -> None:
        replied = SimpleNamespace(text="  replied rules  ", caption=None)
        self.assertEqual(_rules_from_command("/setrules", replied), "replied rules")

    def test_empty_rules_are_rejected(self) -> None:
        self.assertIsNone(_rules_from_command("/setrules   ", None))

    def test_rule_chunks_are_bounded(self) -> None:
        chunks = _telegram_chunks("x" * 7001)
        self.assertEqual([len(chunk) for chunk in chunks], [3500, 3500, 1])


class RulesStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_replace_and_external_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.txt"
            path.write_text("initial rules\n", encoding="utf-8")
            store = RulesStore(path)
            await store.initialize()

            await store.replace("replacement rules")
            self.assertEqual(await store.current_rules(), "replacement rules")
            self.assertEqual(path.with_name("rules.txt.previous").read_text().strip(), "initial rules")

            path.write_text("external rules\n", encoding="utf-8")
            self.assertEqual(await store.current_rules(), "external rules")


class UserStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_retrieve_persist_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "creds.json"
            store = UserStore(path)
            await store.initialize()

            # No credentials initially.
            self.assertIsNone(await store.get(12345))

            # Store and retrieve.
            creds = JiraCredentials(
                jira_username="testuser",
                jira_display_name="Test User",
                jira_pat="TEST_ONLY_PAT",
            )
            await store.store(12345, creds)
            retrieved = await store.get(12345)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.jira_username, "testuser")
            self.assertEqual(retrieved.jira_display_name, "Test User")

            # Persistence: create a new store instance and verify.
            store2 = UserStore(path)
            await store2.initialize()
            retrieved2 = await store2.get(12345)
            self.assertIsNotNone(retrieved2)
            self.assertEqual(retrieved2.jira_username, "testuser")

            # Remove.
            self.assertTrue(await store.remove(12345))
            self.assertIsNone(await store.get(12345))
            self.assertFalse(await store.remove(12345))


class VpnTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manager(
        *,
        enabled: bool = True,
        allow_start: bool = False,
    ) -> NetworkManagerL2tpManager:
        return NetworkManagerL2tpManager(
            enabled=enabled,
            connection_name="TEST_ONLY_CONNECTION",
            profile_path=Path("/TEST_ONLY_PROFILE.nmconnection"),
            allow_start=allow_start,
            nmcli_bin=Path("/usr/bin/nmcli"),
            sudo_bin=Path("/usr/bin/sudo"),
            command_timeout_seconds=1,
        )

    async def test_activated_state_is_up(self) -> None:
        manager = self.manager()

        async def activated(*command: str) -> tuple[int, str]:
            return 0, "GENERAL.STATE:activated\n"

        manager._run_for_state = activated  # type: ignore[method-assign]
        self.assertEqual((await manager.status()).state, VpnState.UP)

    async def test_deactivated_is_not_mistaken_for_activated(self) -> None:
        manager = self.manager()

        async def deactivated(*command: str) -> tuple[int, str]:
            return 0, "GENERAL.STATE:deactivated\n"

        manager._run_for_state = deactivated  # type: ignore[method-assign]
        self.assertEqual((await manager.status()).state, VpnState.DOWN)

    async def test_disabled_state_never_invokes_nmcli(self) -> None:
        manager = self.manager(enabled=False)
        self.assertEqual((await manager.status()).state, VpnState.DISABLED)

    async def test_start_uses_only_the_exact_profile_and_connection(self) -> None:
        manager = self.manager(allow_start=True)
        statuses = iter(
            (
                SimpleNamespace(state=VpnState.DOWN, is_up=False),
                SimpleNamespace(state=VpnState.UP, is_up=True),
            )
        )
        commands: list[tuple[str, ...]] = []

        async def status() -> SimpleNamespace:
            return next(statuses)

        async def run_quiet(*command: str) -> int:
            commands.append(command)
            return 0

        manager.status = status  # type: ignore[method-assign]
        manager._run_quiet = run_quiet  # type: ignore[method-assign]

        result = await manager.start()
        self.assertTrue(result.is_up)
        self.assertEqual(
            commands,
            [
                (
                    str(Path("/usr/bin/sudo")),
                    "-n",
                    str(Path("/usr/bin/nmcli")),
                    "connection",
                    "load",
                    str(Path("/TEST_ONLY_PROFILE.nmconnection")),
                ),
                (
                    str(Path("/usr/bin/sudo")),
                    "-n",
                    str(Path("/usr/bin/nmcli")),
                    "connection",
                    "up",
                    "TEST_ONLY_CONNECTION",
                ),
            ],
        )


class PreviewTests(unittest.TestCase):
    def test_preview_is_bounded_and_review_only(self) -> None:
        template = JiraTaskTemplate(
            summary="Test summary",
            description="d" * 2000,
            issuetype="Task",
            labels=["test"],
            priority="Test priority",
            project_key=None,
            components=[],
            assignee=None,
            acceptance_criteria=["a" * 2000],
        )
        preview = jira_template_preview(template)
        self.assertLessEqual(len(preview), 4000)
        self.assertIn("Jira 工单草稿预览", preview)
        self.assertIn("未指定", preview)

    def test_media_enum_serializes_to_expected_value(self) -> None:
        self.assertEqual(MediaType.PHOTO.value, "photo")

    def test_editable_text_and_parser(self) -> None:
        from dztgbot.analysis import jira_template_editable_text, parse_edited_template
        original = JiraTaskTemplate(
            summary="Original Summary",
            description="Original Description",
            issuetype="Task",
            labels=["test"],
            priority="Medium",
            project_key="NGSSA3",
            components=[],
            assignee=None,
            acceptance_criteria=["Criterion 1"],
        )
        editable = jira_template_editable_text(original)
        self.assertIn("标题: Original Summary", editable)

        edited_input = (
            "标题: Modified Summary\n"
            "类型: 缺陷\n"
            "项目: NGSSA3\n"
            "优先级: High\n"
            "描述:\n"
            "Modified Description line 1\n"
            "Modified Description line 2\n\n"
            "验收标准:\n"
            "- New Criterion 1\n"
            "- New Criterion 2"
        )
        parsed = parse_edited_template(edited_input, original)
        self.assertEqual(parsed.summary, "Modified Summary")
        self.assertEqual(parsed.issuetype, "缺陷")
        self.assertEqual(parsed.priority, "High")
        self.assertIn("Modified Description line 1", parsed.description)
        self.assertEqual(parsed.acceptance_criteria, ["New Criterion 1", "New Criterion 2"])


class GeminiAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_structured_response_is_locally_validated(self) -> None:
        class FakeRulesStore:
            async def current_rules(self) -> str:
                return "TEST_ONLY_RULES"

        class FakeModels:
            async def generate_content(self, **kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    parsed={
                        "summary": "Test summary",
                        "description": "Test description",
                        "issuetype": "Task",
                        "labels": ["test"],
                        "priority": "Test priority",
                        "project_key": None,
                        "components": [],
                        "assignee": None,
                        "acceptance_criteria": ["Test criterion"],
                    },
                    text=None,
                )

        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._model = "TEST_ONLY_MODEL"
        analyzer._timeout_seconds = 1
        analyzer._rules_store = FakeRulesStore()
        analyzer._default_project_key = None
        analyzer._client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
        forwarded = ForwardedMessage(
            original_sender=TelegramIdentity(id=None, display_name="Test sender"),
            original_chat=None,
            text="TEST_ONLY_FORWARDED_TEXT",
            media_type=MediaType.TEXT,
        )

        result = await analyzer.analyze(forwarded)
        self.assertIsInstance(result, JiraTaskTemplate)
        self.assertEqual(result.summary, "Test summary")


class ErrorLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_error_log_does_not_serialize_update_or_error_message(self) -> None:
        update = SimpleNamespace(message="TEST_ONLY_PRIVATE_FORWARDED_TEXT")
        context = SimpleNamespace(error=ValueError("TEST_ONLY_SECRET_ERROR_TEXT"))
        with self.assertLogs("dztgbot.__main__", level="ERROR") as captured:
            await handle_application_error(update, context)

        combined = "\n".join(captured.output)
        self.assertIn("ValueError", combined)
        self.assertNotIn("TEST_ONLY_PRIVATE_FORWARDED_TEXT", combined)
        self.assertNotIn("TEST_ONLY_SECRET_ERROR_TEXT", combined)


if __name__ == "__main__":
    unittest.main()
