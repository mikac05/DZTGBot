"""Unit tests for /standup report and Issue Watcher toggle features.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from dztgbot.domain.models import JiraIssueView, JiraSearchResult
from dztgbot.services.jira_issue_service import JiraIssueService
from dztgbot.ui.keyboards import build_universal_issue_card_keyboard
from dztgbot.ui.rendering import render_standup_report


class TestStandupAndWatch(unittest.IsolatedAsyncioTestCase):
    """Test suite for standup report generation and watcher toggle."""

    def setUp(self) -> None:
        self.mock_gateway = AsyncMock()
        self.mock_user_store = MagicMock()
        self.user_id = 1001

        creds = MagicMock()
        creds.pat = "test_pat_token"
        self.mock_user_store.get_credentials.return_value = creds

        self.service = JiraIssueService(self.mock_gateway, self.mock_user_store)

    async def test_get_standup_summary_grouping(self) -> None:
        blocked_issue = JiraIssueView(
            issue_key="PROJ-1", issue_id="1", summary="Blocked Item", status="Open", priority="High", is_flagged=True
        )
        qa_issue = JiraIssueView(
            issue_key="PROJ-2", issue_id="2", summary="Testing Item", status="In QA", priority="Medium"
        )
        done_issue = JiraIssueView(
            issue_key="PROJ-3", issue_id="3", summary="Done Item", status="Closed", priority="Low"
        )
        dev_issue = JiraIssueView(
            issue_key="PROJ-4", issue_id="4", summary="Dev Item", status="In Progress", priority="High"
        )

        self.mock_gateway.search_jql.return_value = JiraSearchResult(
            total=4, issues=(blocked_issue, qa_issue, done_issue, dev_issue), jql="test"
        )

        blocked, in_progress, in_qa, done = await self.service.get_standup_summary(self.user_id)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].issue_key, "PROJ-1")
        self.assertEqual(len(in_qa), 1)
        self.assertEqual(in_qa[0].issue_key, "PROJ-2")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].issue_key, "PROJ-3")
        self.assertEqual(len(in_progress), 1)
        self.assertEqual(in_progress[0].issue_key, "PROJ-4")

    async def test_watch_and_unwatch_issue(self) -> None:
        sample_issue = JiraIssueView(
            issue_key="PROJ-10", issue_id="10", summary="Sample", status="Open", priority="Medium", is_watching=True
        )
        self.mock_gateway.get_issue_details.return_value = sample_issue

        res_watch = await self.service.watch_issue(self.user_id, "PROJ-10")
        self.assertTrue(res_watch.is_watching)
        self.mock_gateway.watch_issue.assert_called_once_with("PROJ-10", "test_pat_token")

        await self.service.unwatch_issue(self.user_id, "PROJ-10")
        self.mock_gateway.unwatch_issue.assert_called_once_with("PROJ-10", "test_pat_token")

    def test_render_standup_report(self) -> None:
        blocked = (JiraIssueView(issue_key="PROJ-1", issue_id="1", summary="Blocked", status="Open", priority="High"),)
        in_progress = ()
        in_qa = ()
        done = ()

        html_out = render_standup_report(blocked, in_progress, in_qa, done)
        self.assertIn("每日團隊站會摘要", html_out)
        self.assertIn("PROJ-1", html_out)
        self.assertIn("Blocked", html_out)

    def test_keyboard_watch_toggle_button(self) -> None:
        kbd_watch = build_universal_issue_card_keyboard("PROJ-1", "http://jira/1", is_watching=False)
        button_texts = [btn.text for row in kbd_watch.inline_keyboard for btn in row]
        self.assertIn("👁️ Watch", button_texts)

        kbd_unwatch = build_universal_issue_card_keyboard("PROJ-1", "http://jira/1", is_watching=True)
        button_texts_unwatch = [btn.text for row in kbd_unwatch.inline_keyboard for btn in row]
        self.assertIn("👁️ Unwatch", button_texts_unwatch)

    def test_extract_figma_url(self) -> None:
        from dztgbot.ui.keyboards import extract_figma_url

        sample_desc = "Here is the spec: https://www.figma.com/file/xyz123/Login-Flow please review."
        url = extract_figma_url(sample_desc)
        self.assertEqual(url, "https://www.figma.com/file/xyz123/Login-Flow")

        no_figma = "No figma link here"
        self.assertIsNone(extract_figma_url(no_figma))

    def test_keyboard_figma_button(self) -> None:
        figma_url = "https://figma.com/file/xyz123/Design"
        kbd = build_universal_issue_card_keyboard(
            "PROJ-1", "http://jira/1", is_watching=False, figma_url=figma_url
        )
        button_texts = [btn.text for row in kbd.inline_keyboard for btn in row]
        self.assertIn("🎨 Figma Spec ↗", button_texts)


if __name__ == "__main__":
    unittest.main()
