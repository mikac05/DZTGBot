"""Centralized inline and reply keyboard constructors.

Uses strictly bound j1:<action>:<opaque-token> callback data for inline buttons.
Supports native CopyTextButton when permitted by python-telegram-bot version.
"""

from __future__ import annotations

import re
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
from dztgbot.domain.models import JiraIssueView, JiraTransitionView, PublishedIssue
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


def get_unauthenticated_reply_keyboard() -> ReplyKeyboardMarkup:
    """Return main menu reply keyboard for unauthenticated users."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🔑 連結 Jira"),
                KeyboardButton("📖 說明"),
            ],
        ],
        resize_keyboard=True,
    )


def get_authenticated_reply_keyboard() -> ReplyKeyboardMarkup:
    """Return main menu reply keyboard for authenticated users."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📋 指派給我的"),
                KeyboardButton("🚩 我建的"),
            ],
            [
                KeyboardButton("🔍 搜尋"),
                KeyboardButton("📝 新建"),
            ],
            [
                KeyboardButton("🚪 Logout"),
            ],
        ],
        resize_keyboard=True,
    )


def get_remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Return keyboard removal markup."""
    return ReplyKeyboardRemove()


FIGMA_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?figma\.com/(?:file|design|proto|board)/[^\s><\"']+",
    re.IGNORECASE,
)


def extract_figma_url(text: str) -> str | None:
    """Extract the first valid figma.com URL from description or text content."""
    if not text:
        return None
    match = FIGMA_URL_PATTERN.search(text)
    return match.group(0) if match else None


def build_universal_issue_card_keyboard(
    issue_key: str,
    issue_url: str,
    is_blocked: bool = False,
    is_watching: bool = False,
    primary_transition: JiraTransitionView | None = None,
    buttons: ButtonMap | None = None,
    figma_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the universal atomic action bar keyboard for a Jira Issue Card."""
    rows: list[list[InlineKeyboardButton]] = []

    # Row 1: Primary Workflow Transition or Move button
    row1: list[InlineKeyboardButton] = []
    if primary_transition is not None:
        row1.append(
            InlineKeyboardButton(
                f"▶️ {primary_transition.name} ({primary_transition.to_status})",
                callback_data=f"do_mv:{issue_key}:{primary_transition.transition_id}",
            )
        )
    else:
        mv_data = _get_callback_data(buttons, CallbackAction.MOVE_TRANSITION) if buttons else None
        row1.append(InlineKeyboardButton("➡️ Move", callback_data=mv_data or f"card_mv:{issue_key}"))

    qed_data = _get_callback_data(buttons, CallbackAction.QUICK_EDIT) if buttons else None
    row1.append(InlineKeyboardButton("📝 Edit", callback_data=qed_data or f"card_qed:{issue_key}"))

    cmt_data = _get_callback_data(buttons, CallbackAction.ADD_COMMENT) if buttons else None
    row1.append(InlineKeyboardButton("💬 Comment", callback_data=cmt_data or f"card_cmt:{issue_key}"))
    rows.append(row1)

    # Row 2: Frequently used issue controls (Block, Assign, Watch, Sub-task)
    row2: list[InlineKeyboardButton] = []
    if is_blocked:
        ubk_data = _get_callback_data(buttons, CallbackAction.UNBLOCK_ISSUE) if buttons else None
        row2.append(InlineKeyboardButton("✅ Unblock", callback_data=ubk_data or f"card_ubk:{issue_key}"))
    else:
        blk_data = _get_callback_data(buttons, CallbackAction.BLOCK_ISSUE) if buttons else None
        row2.append(InlineKeyboardButton("⚠️ Block", callback_data=blk_data or f"card_blk:{issue_key}"))

    asn_data = _get_callback_data(buttons, CallbackAction.ASSIGN_ISSUE) if buttons else None
    row2.append(InlineKeyboardButton("👤 Assign", callback_data=asn_data or f"card_asn:{issue_key}"))

    if is_watching:
        row2.append(InlineKeyboardButton("👁️ Unwatch", callback_data=f"card_uwtc:{issue_key}"))
    else:
        row2.append(InlineKeyboardButton("👁️ Watch", callback_data=f"card_wtc:{issue_key}"))

    sub_data = _get_callback_data(buttons, CallbackAction.CREATE_SUBTASK) if buttons else None
    row2.append(InlineKeyboardButton("➕ Sub-task", callback_data=sub_data or f"card_sub:{issue_key}"))
    rows.append(row2)

    # Row 3: Open in Jira link & Optional Figma link
    row3: list[InlineKeyboardButton] = [
        InlineKeyboardButton("Open in Jira ↗", url=issue_url)
    ]
    if figma_url:
        row3.append(InlineKeyboardButton("🎨 Figma Spec ↗", url=figma_url))
    rows.append(row3)

    return InlineKeyboardMarkup(rows)


def build_smart_filter_bar_keyboard() -> InlineKeyboardMarkup:
    """Build the quick search filter bar keyboard."""
    row1 = [
        InlineKeyboardButton("My Open", callback_data="flt:my"),
        InlineKeyboardButton("I Created", callback_data="flt:created"),
        InlineKeyboardButton("Unassigned", callback_data="flt:unassigned"),
    ]
    row2 = [
        InlineKeyboardButton("Blocked", callback_data="flt:blocked"),
        InlineKeyboardButton("This Sprint", callback_data="flt:sprint"),
    ]
    return InlineKeyboardMarkup([row1, row2])


def build_paginated_search_keyboard(
    issues: Sequence[JiraIssueView],
    filter_code: str,
    page: int = 1,
    per_page: int = 5,
) -> InlineKeyboardMarkup:
    """Build compact paginated search keyboard with item detail buttons."""
    total_issues = len(issues)
    if total_issues == 0:
        return build_smart_filter_bar_keyboard()

    total_pages = max(1, (total_issues + per_page - 1) // per_page)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * per_page
    end_idx = min(start_idx + per_page, total_issues)
    page_issues = issues[start_idx:end_idx]

    rows: list[list[InlineKeyboardButton]] = []

    # Row 1: Item selection buttons
    item_row: list[InlineKeyboardButton] = []
    for idx, issue in enumerate(page_issues, start=start_idx + 1):
        item_row.append(
            InlineKeyboardButton(
                f"{idx}. {issue.issue_key}",
                callback_data=f"shc:{issue.issue_key}",
            )
        )
    rows.append(item_row)

    # Row 2: Pagination controls
    if total_pages > 1:
        page_row: list[InlineKeyboardButton] = []
        if current_page > 1:
            page_row.append(InlineKeyboardButton("◀️ 上一頁", callback_data=f"pg:{filter_code}:{current_page - 1}"))
        else:
            page_row.append(InlineKeyboardButton(" 」，", callback_data="ignore"))

        page_row.append(InlineKeyboardButton(f"{current_page} / {total_pages}", callback_data="ignore"))

        if current_page < total_pages:
            page_row.append(InlineKeyboardButton("下一頁 ▶️", callback_data=f"pg:{filter_code}:{current_page + 1}"))
        else:
            page_row.append(InlineKeyboardButton(" 」，", callback_data="ignore"))

        rows.append(page_row)

    # Row 3 & 4: Smart Filter Bar
    filter_bar = build_smart_filter_bar_keyboard()
    rows.extend(filter_bar.inline_keyboard)

    return InlineKeyboardMarkup(rows)

