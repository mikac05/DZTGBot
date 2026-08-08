"""Unit tests for UI rendering formatters and keyboard constructors (Task P5-A)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate, PublishedIssue
from dztgbot.services.callback_service import IssuedCallbackButton
from dztgbot.ui.keyboards import (
    build_draft_inline_keyboard,
    build_published_inline_keyboard,
    build_reconcile_inline_keyboard,
    build_retry_inline_keyboard,
    get_draft_reply_keyboard,
    get_remove_reply_keyboard,
)
from dztgbot.ui.rendering import (
    TELEGRAM_MESSAGE_LIMIT,
    html_escape,
    render_cancelled_card,
    render_draft_card,
    render_expired_card,
    render_private_only_warning,
    render_published_card,
    render_published_update_card,
    render_safe_feedback,
    render_submission_error_card,
    render_submission_progress,
    render_unknown_outcome_card,
    truncate_text,
)


class TestUIRenderingFormatters(unittest.TestCase):
    """Test HTML formatters for escaping, structure, and length bounds."""

    def test_html_escape(self) -> None:
        self.assertEqual(html_escape("<script>alert('xss')</script>"), "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;")
        self.assertEqual(html_escape("Foo & Bar"), "Foo &amp; Bar")
        self.assertEqual(html_escape(None), "")

    def test_truncate_text(self) -> None:
        text = "a" * 500
        truncated = truncate_text(text, max_chars=100)
        self.assertEqual(len(truncated), 100)
        self.assertTrue(truncated.endswith("...[truncated]"))

    def test_render_draft_card_escaping_and_content(self) -> None:
        template = JiraTaskTemplate(
            project_key="TEST",
            issue_type="Task",
            summary="Test <Summary> & More",
            description="Test <Description>\nLine 2",
            priority="High",
            labels=("frontend", "ui"),
            components=("core",),
            assignee="alice<user>",
            acceptance_criteria=["AC1 <check>", "AC2"],
        )
        draft = Draft.create_new(owner_id=123, chat_id=456)
        draft = draft.create_new(owner_id=123, chat_id=456)
        draft = draft.__class__(
            draft_id="draft-101",
            owner_id=123,
            chat_id=456,
            state=DraftState.REVIEW,
            revision=1,
            template=template,
            last_error="Err <500>",
        )

        html_out = render_draft_card(draft)
        self.assertIn("Test &lt;Summary&gt; &amp; More", html_out)
        self.assertIn("Test &lt;Description&gt;", html_out)
        self.assertIn("alice&lt;user&gt;", html_out)
        self.assertIn("AC1 &lt;check&gt;", html_out)
        self.assertIn("Err &lt;500&gt;", html_out)
        self.assertLessEqual(len(html_out), TELEGRAM_MESSAGE_LIMIT)

    def test_render_draft_card_overlong_truncation(self) -> None:
        long_desc = "X" * 5000
        template = JiraTaskTemplate(
            project_key="TEST",
            issue_type="Bug",
            summary="Overlong",
            description=long_desc,
            priority="Low",
        )
        draft = Draft(
            draft_id="draft-long",
            owner_id=123,
            chat_id=456,
            state=DraftState.REVIEW,
            revision=1,
            template=template,
        )

        html_out = render_draft_card(draft)
        self.assertLessEqual(len(html_out), TELEGRAM_MESSAGE_LIMIT)

    def test_render_published_card(self) -> None:
        draft = Draft(
            draft_id="draft-pub",
            owner_id=10,
            chat_id=20,
            state=DraftState.COMPLETE,
        )
        issue = PublishedIssue(
            issue_key="TW-999",
            issue_id="10099",
            issue_url="https://jira.example.com/browse/TW-999",
        )

        html_out = render_published_card(draft, issue)
        self.assertIn("TW-999", html_out)
        self.assertIn("https://jira.example.com/browse/TW-999", html_out)
        self.assertIn("draft-pub", html_out)

    def test_render_other_status_cards(self) -> None:
        draft = Draft(
            draft_id="d-123",
            owner_id=1,
            chat_id=2,
            state=DraftState.SUBMISSION_RETRYABLE,
            last_error="ConnectTimeout",
        )

        self.assertIn("草稿已取消", render_cancelled_card(draft))
        self.assertIn("草稿已過期", render_expired_card(draft))
        self.assertIn("ConnectTimeout", render_submission_error_card(draft))
        self.assertIn("調和", render_unknown_outcome_card(draft))
        self.assertIn("工單提交中", render_submission_progress(draft))
        self.assertIn("私訊", render_private_only_warning())
        self.assertIn("Notice:", render_safe_feedback("Hello"))


class TestUIKeyboardConstructors(unittest.TestCase):
    """Test inline and reply keyboard constructors."""

    def test_build_draft_inline_keyboard(self) -> None:
        buttons = {
            CallbackAction.CONFIRM: "j1:cfm:0123456789abcdef0123456789abcdef",
            CallbackAction.TOGGLE_TYPE: "j1:ttyp:0123456789abcdef0123456789abcdef",
            CallbackAction.TOGGLE_PRIORITY: "j1:tpri:0123456789abcdef0123456789abcdef",
            CallbackAction.EDIT: "j1:edt:0123456789abcdef0123456789abcdef",
            CallbackAction.CANCEL: "j1:cnl:0123456789abcdef0123456789abcdef",
        }

        kb = build_draft_inline_keyboard(buttons)
        self.assertIsInstance(kb, InlineKeyboardMarkup)
        self.assertEqual(len(kb.inline_keyboard), 3)

        # Check button callback datas
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, buttons[CallbackAction.CONFIRM])
        self.assertEqual(kb.inline_keyboard[0][1].callback_data, buttons[CallbackAction.EDIT])
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, buttons[CallbackAction.TOGGLE_TYPE])
        self.assertEqual(kb.inline_keyboard[1][1].callback_data, buttons[CallbackAction.TOGGLE_PRIORITY])
        self.assertEqual(kb.inline_keyboard[2][0].callback_data, buttons[CallbackAction.CANCEL])

    def test_build_retry_and_reconcile_keyboards(self) -> None:
        buttons = {
            CallbackAction.RETRY: "j1:rty:token1",
            CallbackAction.RECONCILE: "j1:rcn:token2",
            CallbackAction.CANCEL: "j1:cnl:token3",
        }

        retry_kb = build_retry_inline_keyboard(buttons)
        self.assertEqual(retry_kb.inline_keyboard[0][0].callback_data, "j1:rty:token1")

        reconcile_kb = build_reconcile_inline_keyboard(buttons)
        self.assertEqual(reconcile_kb.inline_keyboard[0][0].callback_data, "j1:rcn:token2")

    def test_build_published_inline_keyboard(self) -> None:
        issue = PublishedIssue(
            issue_key="TW-100",
            issue_id="10100",
            issue_url="https://jira.example.com/browse/TW-100",
        )
        kb = build_published_inline_keyboard(issue)
        self.assertEqual(kb.inline_keyboard[0][0].url, "https://jira.example.com/browse/TW-100")

    def test_reply_keyboards(self) -> None:
        rk = get_draft_reply_keyboard()
        self.assertIsInstance(rk, ReplyKeyboardMarkup)
        self.assertTrue(rk.resize_keyboard)

        rem = get_remove_reply_keyboard()
        self.assertIsInstance(rem, ReplyKeyboardRemove)


if __name__ == "__main__":
    unittest.main()
