"""Security-oriented test sentinels and helpers (Phase 0 / P0-G).

Only approved non-secret sentinels appear here. Never commit real PATs,
passwords, cookies, or Telegram tokens.
"""

from __future__ import annotations

from types import SimpleNamespace

# Approved test-only sentinels (not valid production credentials).
TEST_ONLY_PAT = "TEST_ONLY_NOT_A_REAL_JIRA_PAT_TOKEN_VALUE"
TEST_ONLY_TELEGRAM_TOKEN = "TEST_ONLY_NOT_A_REAL_TELEGRAM_TOKEN"
TEST_ONLY_GEMINI_KEY = "TEST_ONLY_NOT_A_REAL_GEMINI_KEY"
TEST_ONLY_PASSWORD_SHAPE = "alice:not-a-real-password"
TEST_ONLY_BASIC_SHAPE = "Basic dGVzdDp0ZXN0"
TEST_ONLY_COOKIE_SHAPE = "JSESSIONID=TEST_ONLY_NOT_A_REAL_SESSION"


def private_chat(*, chat_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="private", title=None)


def group_chat(*, chat_id: int = -2002, title: str = "Ops Group") -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="supergroup", title=title)


def actor(*, user_id: int = 1001, name: str = "Admin User") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, full_name=name, username="admin_user")


def minimal_env(
    *,
    rules_path: str,
    verify_ssl: str = "true",
    admin_ids: str = "1001",
) -> dict[str, str]:
    """Environment map for Settings tests without real secrets."""

    return {
        "TELEGRAM_BOT_TOKEN": TEST_ONLY_TELEGRAM_TOKEN,
        "GEMINI_API_KEY": TEST_ONLY_GEMINI_KEY,
        "TELEGRAM_ADMIN_USER_IDS": admin_ids,
        "JIRA_RULES_PATH": rules_path,
        "JIRA_URL": "https://jira.test.example.com",
        "JIRA_VERIFY_SSL": verify_ssl,
        "VPN_ENABLED": "false",
        "LOG_LEVEL": "INFO",
    }
