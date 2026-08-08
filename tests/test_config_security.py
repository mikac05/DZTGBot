"""Configuration security and TLS defaults (Phase 4 Task P4-G)."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dztgbot.config import (
    DEFAULT_AUTH_TTL_SECONDS,
    JIRA_VERIFY_DISABLED_WARNING,
    Settings,
)
from tests.support.security_fakes import (
    TEST_ONLY_GEMINI_KEY,
    TEST_ONLY_TELEGRAM_TOKEN,
    minimal_env,
)


def _base_env(rules_path: str, **overrides: str) -> dict[str, str]:
    env = minimal_env(rules_path=rules_path)
    env.update(overrides)
    return env


class JiraUrlSecurityTests(unittest.TestCase):
    def test_https_host_only_url_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules), JIRA_URL="https://jira.test.example.com/")
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.jira_url, "https://jira.test.example.com")

    def test_context_path_is_preserved_without_trailing_slash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules), JIRA_URL="https://jira.test.example.com/jira/")
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.jira_url, "https://jira.test.example.com/jira")

    def test_invalid_jira_urls_are_rejected(self) -> None:
        cases = (
            ("http://jira.test.example.com", "https"),
            ("https://user:" + "pass@jira.test.example.com", "credential"),
            ("https://jira.test.example.com/path#frag", "fragment"),
            ("https://jira.test.example.com/?a=1", "query"),
            ("https:///no-host", "host"),
            ("not-a-url", "https"),
            ("TODO_REPLACE_WITH_JIRA_SERVER_URL", "JIRA_URL"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            for raw_url, needle in cases:
                with self.subTest(url=raw_url):
                    env = _base_env(str(rules), JIRA_URL=raw_url)
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaisesRegex(RuntimeError, needle):
                            Settings.from_environment()


class TlsDefaultTests(unittest.TestCase):
    def test_verify_ssl_defaults_true_and_tls_verify_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules))
            env.pop("JIRA_VERIFY_SSL", None)
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertTrue(settings.jira_verify_ssl)
            self.assertIs(settings.jira_tls_verify, True)
            self.assertIsNone(settings.jira_ca_bundle_path)

    def test_verify_disable_is_explicit_escape_hatch_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules), JIRA_VERIFY_SSL="false")
            with patch.dict(os.environ, env, clear=True):
                with self.assertLogs("dztgbot.config", level=logging.WARNING) as captured:
                    settings = Settings.from_environment()
            self.assertFalse(settings.jira_verify_ssl)
            self.assertIs(settings.jira_tls_verify, False)
            self.assertTrue(
                any(JIRA_VERIFY_DISABLED_WARNING in message for message in captured.output)
            )
            joined = "\n".join(captured.output)
            self.assertNotIn("jira.test.example.com", joined)
            self.assertNotIn(TEST_ONLY_TELEGRAM_TOKEN, joined)
            self.assertNotIn(TEST_ONLY_GEMINI_KEY, joined)

    def test_custom_ca_bundle_requires_verify_and_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            ca_path = Path(tmp) / "private-ca.pem"
            ca_path.write_text("TEST_ONLY_NOT_A_REAL_CA", encoding="utf-8")

            ok_env = _base_env(
                str(rules),
                JIRA_VERIFY_SSL="true",
                JIRA_CA_BUNDLE_PATH=str(ca_path),
            )
            with patch.dict(os.environ, ok_env, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.jira_ca_bundle_path, ca_path)
            self.assertEqual(settings.jira_tls_verify, str(ca_path))

            conflict = _base_env(
                str(rules),
                JIRA_VERIFY_SSL="false",
                JIRA_CA_BUNDLE_PATH=str(ca_path),
            )
            with patch.dict(os.environ, conflict, clear=True):
                with self.assertRaisesRegex(RuntimeError, "JIRA_CA_BUNDLE_PATH"):
                    Settings.from_environment()

            relative = _base_env(
                str(rules),
                JIRA_CA_BUNDLE_PATH="relative/ca.pem",
            )
            with patch.dict(os.environ, relative, clear=True):
                with self.assertRaisesRegex(RuntimeError, "absolute"):
                    Settings.from_environment()


class AuthAndAudiencePolicyTests(unittest.TestCase):
    def test_pat_only_and_private_chat_defaults_are_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            self.assertTrue(settings.auth_pat_only)
            self.assertTrue(settings.private_chat_only)
            self.assertEqual(settings.auth_ttl_seconds, DEFAULT_AUTH_TTL_SECONDS)

    def test_disabling_pat_only_or_private_chat_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            for key, needle in (
                ("AUTH_PAT_ONLY", "AUTH_PAT_ONLY"),
                ("PRIVATE_CHAT_ONLY", "PRIVATE_CHAT_ONLY"),
            ):
                with self.subTest(key=key):
                    env = _base_env(str(rules), **{key: "false"})
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaisesRegex(RuntimeError, needle):
                            Settings.from_environment()

    def test_allowed_user_policy_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")

            empty = _base_env(str(rules))
            with patch.dict(os.environ, empty, clear=True):
                settings = Settings.from_environment()
            self.assertIsNone(settings.telegram_allowed_user_ids)

            allowed = _base_env(str(rules), TELEGRAM_ALLOWED_USER_IDS="1001,2002")
            with patch.dict(os.environ, allowed, clear=True):
                settings = Settings.from_environment()
            self.assertEqual(settings.telegram_allowed_user_ids, frozenset({1001, 2002}))

            bad = _base_env(str(rules), TELEGRAM_ALLOWED_USER_IDS="0,-1")
            with patch.dict(os.environ, bad, clear=True):
                with self.assertRaisesRegex(RuntimeError, "TELEGRAM_ALLOWED_USER_IDS"):
                    Settings.from_environment()


class SecretSafetyTests(unittest.TestCase):
    def test_settings_repr_hides_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.txt"
            rules.write_text("rules", encoding="utf-8")
            env = _base_env(str(rules))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_environment()
            rendered = repr(settings)
            self.assertNotIn(TEST_ONLY_TELEGRAM_TOKEN, rendered)
            self.assertNotIn(TEST_ONLY_GEMINI_KEY, rendered)


if __name__ == "__main__":
    unittest.main()
