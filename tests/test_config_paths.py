"""Configuration path and numeric-bound validation (Phase 4 Task P4-G)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dztgbot.config import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_ATTACHMENT_COUNT,
    DEFAULT_MAX_BATCH_MESSAGES,
    DEFAULT_MAX_CONCURRENT_GEMINI,
    DEFAULT_MAX_CONCURRENT_JIRA,
    DEFAULT_MAX_MESSAGE_CHARACTERS,
    DEFAULT_MAX_PROMPT_CHARACTERS,
    DEFAULT_MAX_QUEUE_SIZE,
    Settings,
    _repository_root,
)
from tests.support.security_fakes import minimal_env


def _base_env(rules_path: str, **overrides: str) -> dict[str, str]:
    env = minimal_env(rules_path=rules_path)
    env.update(overrides)
    return env


class WorkflowDbPathTests(unittest.TestCase):
    def test_absent_workflow_db_path_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertIsNone(settings.workflow_db_path)

    def test_absolute_local_path_outside_checkout_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            db_path = Path(tmp) / "runtime" / "workflow.sqlite3"
            env = _base_env(str(rules), WORKFLOW_DB_PATH=str(db_path))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.workflow_db_path, db_path)

    def test_relative_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules), WORKFLOW_DB_PATH="var/workflow.sqlite3")
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "absolute"):
                    Settings.from_environment()

    def test_path_inside_git_checkout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            inside = _repository_root() / "var" / "workflow.sqlite3"
            env = _base_env(str(rules), WORKFLOW_DB_PATH=str(inside))
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "Git checkout"):
                    Settings.from_environment()

    def test_cloud_synced_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            # Absolute-looking path containing a sync marker segment.
            if os.name == "nt":
                synced = Path(r"C:\Users\test\OneDrive\dztgbot\workflow.sqlite3")
            else:
                synced = Path("/home/test/OneDrive/dztgbot/workflow.sqlite3")
            env = _base_env(str(rules), WORKFLOW_DB_PATH=str(synced))
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "cloud-synced|OneDrive"):
                    Settings.from_environment()


class OptionalPathTests(unittest.TestCase):
    def test_absent_optional_paths_are_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules), VPN_ENABLED="false")
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertIsNone(settings.vpn_profile_path)
            self.assertIsNone(settings.jira_ca_bundle_path)
            self.assertIsNone(settings.jira_default_project_key)
            self.assertIsNone(settings.workflow_db_path)

    def test_todo_vpn_profile_is_treated_as_absent_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(
                str(rules),
                VPN_ENABLED="false",
                VPN_PROFILE_PATH="TODO_REPLACE_WITH_ABSOLUTE_PRIVATE_NETWORKMANAGER_PROFILE_PATH",
                VPN_CONNECTION_NAME="TODO_REPLACE_WITH_NETWORKMANAGER_CONNECTION_NAME",
            )
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertIsNone(settings.vpn_profile_path)
            self.assertEqual(settings.vpn_connection_name, "")

    def test_user_credentials_default_remains_beside_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(
                settings.user_credentials_path,
                Path(tmp) / "user_credentials.json",
            )

    def test_user_credentials_explicit_path_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            creds = Path(tmp) / "state" / "creds.json"
            env = _base_env(str(rules), USER_CREDENTIALS_PATH=str(creds))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.user_credentials_path, creds)

    def test_vpn_enabled_requires_absolute_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(
                str(rules),
                VPN_ENABLED="true",
                VPN_CONNECTION_NAME="dztgbot-l2tp",
                VPN_PROFILE_PATH="relative/profile.nmconnection",
            )
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "VPN_PROFILE_PATH"):
                    Settings.from_environment()


class ProjectKeyAndNumericBoundTests(unittest.TestCase):
    def test_project_key_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")

            ok = _base_env(str(rules), JIRA_DEFAULT_PROJECT_KEY="ngssa3")
            with patch.dict(os.environ, ok, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.jira_default_project_key, "NGSSA3")

            for bad in ("1ABC", "A", "AB-CD", "toolongprojectkey12345", "ab cd"):
                with self.subTest(project_key=bad):
                    env = _base_env(str(rules), JIRA_DEFAULT_PROJECT_KEY=bad)
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaisesRegex(
                            RuntimeError, "JIRA_DEFAULT_PROJECT_KEY"
                        ):
                            Settings.from_environment()

    def test_default_resource_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.max_batch_messages, DEFAULT_MAX_BATCH_MESSAGES)
            self.assertEqual(
                settings.max_message_characters, DEFAULT_MAX_MESSAGE_CHARACTERS
            )
            self.assertEqual(
                settings.max_prompt_characters, DEFAULT_MAX_PROMPT_CHARACTERS
            )
            self.assertEqual(settings.max_attachment_bytes, DEFAULT_MAX_ATTACHMENT_BYTES)
            self.assertEqual(settings.max_attachment_count, DEFAULT_MAX_ATTACHMENT_COUNT)
            self.assertEqual(settings.max_queue_size, DEFAULT_MAX_QUEUE_SIZE)
            self.assertEqual(
                settings.max_concurrent_gemini, DEFAULT_MAX_CONCURRENT_GEMINI
            )
            self.assertEqual(settings.max_concurrent_jira, DEFAULT_MAX_CONCURRENT_JIRA)

    def test_numeric_bounds_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            cases = (
                ("AUTH_TTL_SECONDS", "30"),
                ("AUTH_TTL_SECONDS", "901"),
                ("MAX_BATCH_MESSAGES", "0"),
                ("MAX_BATCH_MESSAGES", "51"),
                ("MAX_MESSAGE_CHARACTERS", "50"),
                ("MAX_PROMPT_CHARACTERS", "500"),
                ("MAX_ATTACHMENT_BYTES", "0"),
                ("MAX_ATTACHMENT_COUNT", "0"),
                ("MAX_QUEUE_SIZE", "0"),
                ("MAX_CONCURRENT_GEMINI", "0"),
                ("MAX_CONCURRENT_JIRA", "17"),
                ("TELEGRAM_CONCURRENT_UPDATES", "0"),
                ("GEMINI_TIMEOUT_SECONDS", "0"),
            )
            for name, value in cases:
                with self.subTest(name=name, value=value):
                    env = _base_env(str(rules), **{name: value})
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(RuntimeError):
                            Settings.from_environment()

    def test_prompt_budget_must_cover_message_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(
                str(rules),
                MAX_MESSAGE_CHARACTERS="8000",
                MAX_PROMPT_CHARACTERS="4000",
            )
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "MAX_PROMPT_CHARACTERS"):
                    Settings.from_environment()


if __name__ == "__main__":
    unittest.main()
