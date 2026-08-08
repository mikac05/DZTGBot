"""Pure HTML formatters for Telegram messages.

Contains zero provider SDK or infrastructure dependencies.
All dynamic content is strictly escaped via html.escape and output length
is bounded to ensure compatibility with Telegram's 4096-character message limit.
"""

from __future__ import annotations

import html
from typing import Mapping, Sequence

from dztgbot.domain.models import Draft, PublishedIssue, JiraTaskTemplate


TELEGRAM_MESSAGE_LIMIT = 4096


def html_escape(text: str | None) -> str:
    """Safely escape text for HTML parse mode."""
    if text is None:
        return ""
    return html.escape(str(text))


def truncate_text(text: str, max_chars: int = 1000) -> str:
    """Truncate text safely before HTML formatting."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 14] + "...[truncated]"


def render_draft_card(draft: Draft, title: str | None = None) -> str:
    """Render a comprehensive draft preview card in HTML format."""
    header = title or f"<b>📋 工單草稿 Preview (Rev {draft.revision})</b>"
    lines = [
        header,
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>",
        f"<b>狀態:</b> <code>{html_escape(draft.state.value)}</code>",
    ]

    template = draft.template
    if template is not None:
        lines.append(f"<b>Project:</b> <code>{html_escape(template.project_key)}</code>")
        lines.append(f"<b>Type:</b> <code>{html_escape(template.issue_type)}</code>")
        lines.append(f"<b>Priority:</b> <code>{html_escape(template.priority)}</code>")
        lines.append(f"<b>Summary:</b> {html_escape(template.summary)}")

        if template.description:
            clean_desc = html_escape(truncate_text(template.description, 800))
            lines.append(f"<b>Description:</b>\n<pre>{clean_desc}</pre>")

        if template.labels:
            labels_str = ", ".join(html_escape(lbl) for lbl in template.labels)
            lines.append(f"<b>Labels:</b> {labels_str}")

        if template.components:
            comp_str = ", ".join(html_escape(c) for c in template.components)
            lines.append(f"<b>Components:</b> {comp_str}")

        if template.assignee:
            lines.append(f"<b>Assignee:</b> <code>{html_escape(template.assignee)}</code>")

        if template.acceptance_criteria:
            criteria = "\n".join(
                f"• {html_escape(ac)}" for ac in template.acceptance_criteria
            )
            lines.append(f"<b>Acceptance Criteria:</b>\n{criteria}")

    if draft.attachments:
        lines.append(f"<b>附件數量:</b> {len(draft.attachments)} 個檔案")

    if draft.last_error:
        lines.append(
            f"<b>最近錯誤:</b> <code>{html_escape(draft.last_error)}</code>"
        )

    full_output = "\n".join(lines)
    if len(full_output) > TELEGRAM_MESSAGE_LIMIT:
        return full_output[: TELEGRAM_MESSAGE_LIMIT - 20] + "\n...[truncated]"
    return full_output


def render_submission_progress(draft: Draft) -> str:
    """Render a submission in-progress notification card."""
    return (
        f"<b>🚀 工單提交中...</b>\n"
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>\n"
        f"請稍候，正在與 Jira 伺服器通訊。"
    )


def render_published_card(draft: Draft, issue: PublishedIssue) -> str:
    """Render a successful issue publication card."""
    lines = [
        "<b>🎉 工單已成功建立！</b>\n",
        f"<b>Issue Key:</b> <a href=\"{html_escape(issue.issue_url)}\"><b>{html_escape(issue.issue_key)}</b></a>",
        f"<b>Issue ID:</b> <code>{html_escape(issue.issue_id)}</code>",
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>",
    ]
    if draft.template:
        lines.append(f"<b>Summary:</b> {html_escape(draft.template.summary)}")

    if draft.attachments:
        lines.append(f"<b>附件處理:</b> 已處理 {len(draft.attachments)} 個附件")

    return "\n".join(lines)


def render_published_update_card(
    draft: Draft, changed_fields: Mapping[str, object]
) -> str:
    """Render a published issue update review card."""
    issue_key = (
        draft.published_issue.issue_key if draft.published_issue else "UNKNOWN"
    )
    lines = [
        f"<b>📝 變更發布工單確認: {html_escape(issue_key)}</b>",
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>",
        "<b>變更欄位:</b>",
    ]
    for key, value in changed_fields.items():
        lines.append(f"• <code>{html_escape(key)}</code>: {html_escape(str(value))}")

    return "\n".join(lines)


def render_cancelled_card(draft: Draft) -> str:
    """Render a draft cancellation card."""
    return (
        f"<b>❌ 工單草稿已取消。</b>\n"
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>"
    )


def render_expired_card(draft: Draft) -> str:
    """Render an expired draft card."""
    return (
        f"<b>⏰ 工單草稿已過期。</b>\n"
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>"
    )


def render_submission_error_card(
    draft: Draft, error_summary: str | None = None
) -> str:
    """Render a retryable submission failure card."""
    err = error_summary or draft.last_error or "未知的網路或伺服器錯誤"
    return (
        f"<b>⚠️ 工單提交失敗（可重試）</b>\n"
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>\n"
        f"<b>錯誤原因:</b> <code>{html_escape(err)}</code>\n\n"
        f"您可以點擊下方按鈕重試提交或取消草稿。"
    )


def render_unknown_outcome_card(draft: Draft) -> str:
    """Render an ambiguous submission outcome / reconciliation required card."""
    err = draft.last_error or "連線逾時，提交結果未知"
    return (
        f"<b>❓ 工單提交狀態未知（需調查調和）</b>\n"
        f"<b>Draft ID:</b> <code>{html_escape(draft.draft_id)}</code>\n"
        f"<b>細節:</b> <code>{html_escape(err)}</code>\n\n"
        f"為了防止重複建立工單，請點擊「調查/調和」按鈕確認 Jira 端的建立結果。"
    )


def render_safe_feedback(message: str) -> str:
    """Render a safe user feedback message."""
    return f"<b>Notice:</b> {html_escape(message)}"


def render_private_only_warning() -> str:
    """Render a private chat enforcement warning."""
    return "⚠️ <b>安全提示:</b> 此機器人僅限私訊（Private Chat）使用。"
