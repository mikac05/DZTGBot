"""Unit tests for multi-language UI (zh_TW, en, zh_CN) and Simplified Chinese issue creation enforcement.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
import asyncio

from dztgbot.ui.i18n import get_text
from dztgbot.user_store import UserStore
from dztgbot.infrastructure.gemini_gateway import GeminiGateway
from dztgbot.domain.models import SourceMessageRef, MediaKind


class TestI18nAndLanguage(unittest.TestCase):
    """Test suite for UI localization and Simplified Chinese issue creation enforcement."""

    def test_i18n_translation_keys_across_languages(self) -> None:
        # Traditional Chinese
        self.assertEqual(get_text("btn_auth", "zh_TW"), "🔑 連結 Jira")
        self.assertEqual(get_text("btn_my_issues", "zh_TW"), "📋 指派給我的")
        self.assertEqual(get_text("btn_language", "zh_TW"), "🌐 語言設置")

        # English
        self.assertEqual(get_text("btn_auth", "en"), "🔑 Link Jira")
        self.assertEqual(get_text("btn_my_issues", "en"), "📋 Assigned to Me")
        self.assertEqual(get_text("btn_language", "en"), "🌐 Language")

        # Simplified Chinese
        self.assertEqual(get_text("btn_auth", "zh_CN"), "🔑 绑定 Jira")
        self.assertEqual(get_text("btn_my_issues", "zh_CN"), "📋 指派给我的")
        self.assertEqual(get_text("btn_language", "zh_CN"), "🌐 语言设置")

    def test_gemini_prompt_enforces_simplified_chinese_issue_content(self) -> None:
        gateway = GeminiGateway(api_key="fake-key-for-test-prompt")
        messages = [SourceMessageRef(message_id=1, chat_id=1, sender_id=1, text="Feature request: Add dark mode", media_kind=MediaKind.TEXT)]
        prompt = gateway.build_prompt(messages, rules_text="Project BOT")

        # Must explicitly instruct Gemini to output summary & description in Simplified Chinese
        self.assertIn("Simplified Chinese (China Chinese / 简体中文)", prompt)

    def test_user_store_persists_language_preference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = Path(temp_dir) / "user_credentials.json"
            store = UserStore(store_path)

            async def run_test():
                await store.initialize()
                # Default language should be zh_TW
                self.assertEqual(await store.get_language(1001), "zh_TW")

                # Set language to English
                await store.set_language(1001, "en")
                self.assertEqual(await store.get_language(1001), "en")

                # Set language to Simplified Chinese
                await store.set_language(1002, "zh_CN")
                self.assertEqual(await store.get_language(1002), "zh_CN")

                # Reload store from disk to verify durable persistence
                store2 = UserStore(store_path)
                await store2.initialize()
                self.assertEqual(await store2.get_language(1001), "en")
                self.assertEqual(await store2.get_language(1002), "zh_CN")

            asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
