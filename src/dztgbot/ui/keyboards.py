"""Centralized inline and reply keyboard constructors.

Uses strictly bound j1:<action>:<opaque-token> callback data for inline buttons.
Supports native CopyTextButton when permitted by python-telegram-bot version.
"""

from __future__ import annotations

from typing import Mapping, Sequence, Union

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

try:
    from telegram import CopyTextButton
    HAS_COPY_TEXT_BUTTON = True
except ImportError:
    HAS_COPY_TEXT_BUTTON = False

from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.models import PublishedIssue
from dztgbot.services.callback_service import IssuedCallbackButton


ButtonMap = Mapping[CallbackAction, Union[IssuedCallbackButton, str]]


def _get_callback_data(buttons: ButtonMap, action: CallbackAction) -> str | None:
    item = buttons.get(action)
    if item is None:
        return None
    if isinstance(item, IssuedCallbackButton):
        return item.callback_data
    return str(item)


def build_draft_inline_keyboard(buttons: ButtonMap) -> InlineKeyboardMarkup:
    """Build the standard 3-row inline keyboard for a draft preview."""
    rows: list[list[InlineKeyboardButton]] = []

    # Row 1: Confirm & Edit
    row1: list[InlineKeyboardButton] = []
    cfm_data = _get_callback_data(buttons, CallbackAction.CONFIRM)
    if cfm_data:
        row1.append(InlineKeyboardButton("✅ 確定提交", callback_data=cfm_data))
    edt_data = _get_callback_data(buttons, CallbackAction.EDIT)
    if edt_data:
        row1.append(InlineKeyboardButton("✏️ 編輯欄位", callback_data=edt_data))
    if row1:
        rows.append(row1)

    # Row 2: Toggle Type & Toggle Priority
    row2: list[InlineKeyboardButton] = []
    ttyp_data = _get_callback_data(buttons, CallbackAction.TOGGLE_TYPE)
    if ttyp_data:
        row2.append(InlineKeyboardButton("🏷️ 切換類型", callback_data=ttyp_data))
    tpri_data = _get_callback_data(buttons, CallbackAction.TOGGLE_PRIORITY)
    if tpri_data:
        row2.append(InlineKeyboardButton("⚡ 切換優先級", callback_data=tpri_data))
    if row2:
        rows.append(row2)

    # Row 3: Cancel
    row3: list[InlineKeyboardButton] = []
    cnl_data = _get_callback_data(buttons, CallbackAction.CANCEL)
    if cnl_data:
        row3.append(InlineKeyboardButton("❌ 取消草稿", callback_data=cnl_data))
    if row3:
        rows.append(row3)

    return InlineKeyboardMarkup(rows)


def build_retry_inline_keyboard(buttons: ButtonMap) -> InlineKeyboardMarkup:
    """Build retry/cancel keyboard for SUBMISSION_RETRYABLE drafts."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    rty_data = _get_callback_data(buttons, CallbackAction.RETRY)
    if rty_data:
        row.append(InlineKeyboardButton("🔄 重試提交", callback_data=rty_data))

    cnl_data = _get_callback_data(buttons, CallbackAction.CANCEL)
    if cnl_data:
        row.append(InlineKeyboardButton("❌ 取消草稿", callback_data=cnl_data))

    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_reconcile_inline_keyboard(buttons: ButtonMap) -> InlineKeyboardMarkup:
    """Build reconcile/cancel keyboard for SUBMISSION_UNKNOWN drafts."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    rcn_data = _get_callback_data(buttons, CallbackAction.RECONCILE)
    if rcn_data:
        row.append(InlineKeyboardButton("🔍 調查/調和", callback_data=rcn_data))

    cnl_data = _get_callback_data(buttons, CallbackAction.CANCEL)
    if cnl_data:
        row.append(InlineKeyboardButton("❌ 取消", callback_data=cnl_data))

    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_published_inline_keyboard(
    issue: PublishedIssue,
    buttons: ButtonMap | None = None,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for a published Jira issue."""
    row1: list[InlineKeyboardButton] = [
        InlineKeyboardButton("🔗 View in Jira", url=issue.issue_url)
    ]

    if HAS_COPY_TEXT_BUTTON:
        row1.append(
            InlineKeyboardButton(
                "📋 Copy Key",
                copy_text=CopyTextButton(text=issue.issue_key),
            )
        )

    rows: list[list[InlineKeyboardButton]] = [row1]

    if buttons is not None:
        edp_data = _get_callback_data(buttons, CallbackAction.EDIT_PUBLISHED)
        if edp_data:
            rows.append(
                [InlineKeyboardButton("📝 編輯發布工單", callback_data=edp_data)]
            )

    return InlineKeyboardMarkup(rows)


def get_draft_reply_keyboard() -> ReplyKeyboardMarkup:
    """Return interactive draft menu keyboard for quick reply buttons."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🏷️ 類型: Task"),
                KeyboardButton("🏷️ 類型: 缺陷"),
            ],
            [
                KeyboardButton("⚡ 優先級: High"),
                KeyboardButton("⚡ 優先級: Medium"),
            ],
            [
                KeyboardButton("✅ 確定提交工單"),
                KeyboardButton("❌ 取消草稿"),
            ],
        ],
        resize_keyboard=True,
    )


def get_remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Return keyboard removal markup."""
    return ReplyKeyboardRemove()
