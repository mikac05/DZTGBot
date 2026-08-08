"""Unit tests for JiraIssueService, CardTrackerService, and Jira 8.4.1 triage features.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from dztgbot.domain.models import JiraIssueView, JiraSearchResult, JiraTransitionView
from dztgbot.services.jira_issue_service import JiraIssueService
from dztgbot.services.card_tracker_service import CardTrackerService
from dztgbot.ui.rendering import render_issue_card, render_search_results
from dztgbot.ui.keyboards import build_universal_issue_card_keyboard, build_smart_filter_bar_keyboard


class TestJiraIssueService(unittest.IsolatedAsyncioTestCase):
    """Test suite for JiraIssueService and live triage actions."""

    def setUp(self) -> None:
        self.mock_gateway = AsyncMock()
        self.mock_user_store = MagicMock()
        self.user_id = 1001

        # Setup user store credentials
        creds = MagicMock()
        creds.pat = "test_pat_token"
        self.mock_user_store.get_credentials.return_value = creds

        self.service = JiraIssueService(self.mock_gateway, self.mock_user_store)

    async def test_search_my_open(self) -> None:
        sample_issue = JiraIssueView(
            issue_key="PROJ-1",
            issue_id="10001",
            summary="Test Issue",
            status="In Progress",
            priority="High",
            assignee="Alex",
            reporter="User",
        )
        self.mock_gateway.search_jql.return_value = JiraSearchResult(
            total=1, issues=(sample_issue,), jql="assignee = currentUser()"
        )

        res = await self.service.search_my_open(self.user_id)
        self.assertEqual(res.total, 1)
        self.assertEqual(res.issues[0].issue_key, "PROJ-1")
        self.mock_gateway.search_jql.assert_called_once()

    async def test_get_transitions(self) -> None:
        sample_trans = JiraTransitionView(
            transition_id="21", name="In Progress", to_status="In Progress"
        )
        self.mock_gateway.get_transitions.return_value = (sample_trans,)

        res = await self.service.get_transitions(self.user_id, "PROJ-1")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].name, "In Progress")

    async def test_execute_transition(self) -> None:
        sample_issue = JiraIssueView(
            issue_key="PROJ-1",
            issue_id="10001",
            summary="Test Issue",
            status="In Progress",
            priority="High",
        )
        self.mock_gateway.get_issue_details.return_value = sample_issue

        res = await self.service.execute_transition(self.user_id, "PROJ-1", "21")
        self.assertEqual(res.status, "In Progress")
        self.mock_gateway.execute_transition.assert_called_once()

    async def test_block_issue(self) -> None:
        sample_issue = JiraIssueView(
            issue_key="PROJ-1",
            issue_id="10001",
            summary="Test Issue",
            status="Open",
            priority="High",
            is_flagged=True,
            blocker_keys=("PROJ-2",),
        )
        self.mock_gateway.get_issue_details.return_value = sample_issue

        res = await self.service.block_issue(self.user_id, "PROJ-1", "PROJ-2", reason="Waiting API")
        self.assertTrue(res.is_flagged)
        self.mock_gateway.block_issue.assert_called_once_with("PROJ-1", "PROJ-2", "test_pat_token", reason="Waiting API")

    async def test_assign_issue(self) -> None:
        sample_issue = JiraIssueView(
            issue_key="PROJ-1",
            issue_id="10001",
            summary="Test Issue",
            status="Open",
            priority="High",
            assignee="alex",
        )
        self.mock_gateway.get_issue_details.return_value = sample_issue

        res = await self.service.assign_issue(self.user_id, "PROJ-1", "alex")
        self.assertEqual(res.assignee, "alex")
        self.mock_gateway.assign_issue.assert_called_once_with("PROJ-1", "alex", "test_pat_token")

    async def test_add_comment(self) -> None:
        sample_issue = JiraIssueView(
            issue_key="PROJ-1",
            issue_id="10001",
            summary="Test Issue",
            status="Open",
            priority="High",
            last_comment_summary="user: Test note",
        )
        self.mock_gateway.get_issue_details.return_value = sample_issue

        res = await self.service.add_comment(self.user_id, "PROJ-1", "Test note")
        self.assertEqual(res.issue_key, "PROJ-1")
        self.mock_gateway.add_comment.assert_called_once_with("PROJ-1", "Test note\n\n*(via Telegram)*", "test_pat_token")

    async def test_unauthenticated_user_raises(self) -> None:
        self.mock_user_store.get_credentials.return_value = None
        with self.assertRaises(ValueError):
            await self.service.get_issue(999, "PROJ-1")


class TestCardTrackerService(unittest.IsolatedAsyncioTestCase):
    """Test suite for CardTrackerService."""

    async def test_register_and_refresh(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.get_card_messages_for_issue.return_value = (
            (100, 1, 1001),
            (200, 2, 1002),
        )
        tracker = CardTrackerService(mock_repo)

        await tracker.register_card(100, 1, "PROJ-1", 1001)
        mock_repo.register_card_message.assert_called_once_with(100, 1, "PROJ-1", 1001)

        sample_issue = JiraIssueView(
            issue_key="PROJ-1",
            issue_id="10001",
            summary="Test Issue",
            status="In Progress",
            priority="High",
        )

        refreshed_chats = []

        async def dummy_updater(chat_id: int, message_id: int, issue: JiraIssueView) -> None:
            refreshed_chats.append((chat_id, message_id, issue.issue_key))

        await tracker.refresh_issue_cards(sample_issue, dummy_updater)
        self.assertEqual(len(refreshed_chats), 2)
        self.assertEqual(refreshed_chats[0], (100, 1, "PROJ-1"))
        self.assertEqual(refreshed_chats[1], (200, 2, "PROJ-1"))


class TestCardRenderingAndKeyboards(unittest.TestCase):
    """Test suite for HTML card rendering and keyboard builders."""

    def test_render_issue_card(self) -> None:
        issue = JiraIssueView(
            issue_key="PROJ-100",
            issue_id="100",
            summary="Fix Login Page Bug",
            status="In Progress",
            priority="High",
            assignee="Alex",
            reporter="User",
            epic_key="PROJ-10",
            sprint_name="Sprint 12",
            is_flagged=True,
            blocker_keys=("PROJ-99",),
            issue_url="https://jira.example.com/browse/PROJ-100",
            last_comment_summary="Alex: Fixed styling",
        )

        card_html = render_issue_card(issue)
        self.assertIn("PROJ-100", card_html)
        self.assertIn("In Progress", card_html)
        self.assertIn("Fix Login Page Bug", card_html)
        self.assertIn("Assignee: Alex", card_html)
        self.assertIn("Epic: PROJ-10", card_html)
        self.assertIn("Blocked by PROJ-99", card_html)
        self.assertIn("Alex: Fixed styling", card_html)

    def test_build_universal_issue_card_keyboard(self) -> None:
        keyboard = build_universal_issue_card_keyboard(
            issue_key="PROJ-100",
            issue_url="https://jira.example.com/browse/PROJ-100",
            is_blocked=False,
        )
        self.assertIsNotNone(keyboard)
        button_texts = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("➡️ Move", button_texts)
        self.assertIn("📝 Edit", button_texts)
        self.assertIn("💬 Comment", button_texts)
        self.assertIn("⚠️ Block", button_texts)
        self.assertIn("👤 Assign", button_texts)
        self.assertIn("➕ Sub-task", button_texts)
        self.assertIn("Open in Jira ↗", button_texts)

    def test_build_smart_filter_bar_keyboard(self) -> None:
        keyboard = build_smart_filter_bar_keyboard()
        button_texts = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("My Open", button_texts)
        self.assertIn("I Created", button_texts)
        self.assertIn("Unassigned", button_texts)
        self.assertIn("Blocked", button_texts)
        self.assertIn("This Sprint", button_texts)


if __name__ == "__main__":
    unittest.main()
