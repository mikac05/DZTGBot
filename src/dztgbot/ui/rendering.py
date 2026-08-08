"""Pure HTML formatters for Telegram messages.

Contains zero provider SDK or infrastructure dependencies.
All dynamic content is strictly escaped via html.escape and output length
is bounded to ensure compatibility with Telegram's 4096-character message limit.
"""

from __future__ import annotations

import html
from typing import Mapping, Sequence

from dztgbot.domain.models import (
    Draft,
    JiraIssueView,
    JiraSearchResult,
    JiraTaskTemplate,
    PublishedIssue,
)


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


def render_issue_card(issue: JiraIssueView, header_title: str | None = None) -> str:
    """Render the universal atomic Jira issue card in HTML format."""
    status_bullet = "●"
    flagged_tag = " · <b>Flagged</b>" if issue.is_flagged else ""
    blocker_str = (
        f"\n⚠️ <b>Blocked by {html_escape(', '.join(issue.blocker_keys))}</b>{flagged_tag}"
        if issue.blocker_keys
        else ("\n⚠️ <b>Flagged (Impediment)</b>" if issue.is_flagged else "")
    )

    assignee_str = html_escape(issue.assignee) if issue.assignee else "<i>Unassigned</i>"
    reporter_str = html_escape(issue.reporter) if issue.reporter else "<i>Unknown</i>"

    context_line = []
    if issue.epic_key:
        context_line.append(f"Epic: {html_escape(issue.epic_key)}")
    if issue.sprint_name:
        context_line.append(f"Sprint: {html_escape(issue.sprint_name)}")
    context_str = f"\n{' | '.join(context_line)}" if context_line else ""

    last_comment = (
        f"\n💬 <i>{html_escape(issue.last_comment_summary)}</i>"
        if issue.last_comment_summary
        else ""
    )

    header = f"<b>{html_escape(header_title)}</b>\n" if header_title else ""
    card_html = (
        f"{header}"
        f"<b><a href=\"{html_escape(issue.issue_url)}\">{html_escape(issue.issue_key)}</a></b>  {status_bullet}  <b>{html_escape(issue.status)}</b>  {status_bullet}  <b>{html_escape(issue.priority)}</b>\n"
        f"<b>{html_escape(issue.summary)}</b>\n"
        f"Assignee: {assignee_str} | Reporter: {reporter_str}"
        f"{context_str}"
        f"{blocker_str}"
        f"{last_comment}"
    )
    return card_html


def render_search_results(result: JiraSearchResult, title: str) -> str:
    """Render a header for JQL search results."""
    count = len(result.issues)
    if count == 0:
        return f"🔍 <b>{html_escape(title)}</b>\n\n<i>No open issues found matching query.</i>"
    return f"🔍 <b>{html_escape(title)}</b> (Found {result.total} open issues, showing {count}):"


def render_compact_search_list(
    result: JiraSearchResult, title: str, page: int = 1, per_page: int = 5
) -> str:
    """Render a compact search result list (Key + Summary) with page header."""
    total_issues = len(result.issues)
    if total_issues == 0:
        return f"🔍 <b>{html_escape(title)}</b>\n\n<i>未找到符合條件的未解決工單。</i>"

    total_pages = max(1, (total_issues + per_page - 1) // per_page)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * per_page
    end_idx = min(start_idx + per_page, total_issues)
    page_issues = result.issues[start_idx:end_idx]

    lines = [
        f"🔍 <b>{html_escape(title)}</b> (共 {result.total} 筆，第 {current_page}/{total_pages} 頁):\n"
    ]
    for idx, issue in enumerate(page_issues, start=start_idx + 1):
        summary_short = truncate_text(issue.summary, 45)
        lines.append(
            f"{idx}. <b><a href=\"{html_escape(issue.issue_url)}\">{html_escape(issue.issue_key)}</a></b>: {html_escape(summary_short)}"
        )

    lines.append("\n<i>點擊下方數字按鈕查看工單詳細卡片與操作介面:</i>")
    return "\n".join(lines)


def render_standup_report(
    blocked: Sequence[JiraIssueView],
    in_progress: Sequence[JiraIssueView],
    in_qa: Sequence[JiraIssueView],
    done: Sequence[JiraIssueView],
) -> str:
    """Render an executive daily standup summary report."""
    lines = ["📊 <b>每日團隊站會摘要 (Daily Standup Summary)</b>\n"]

    # Section 1: Blocked
    lines.append(f"🔴 <b>阻礙中 (Blocked - {len(blocked)} 筆):</b>")
    if blocked:
        for issue in blocked:
            lines.append(
                f"• <b><a href=\"{html_escape(issue.issue_url)}\">{html_escape(issue.issue_key)}</a></b>: "
                f"{html_escape(truncate_text(issue.summary, 40))} ({html_escape(issue.assignee or 'Unassigned')})"
            )
    else:
        lines.append("<i>無阻礙項目</i>")

    lines.append("")

    # Section 2: In Progress
    lines.append(f"🔵 <b>進行中 (In Progress - {len(in_progress)} 筆):</b>")
    if in_progress:
        for issue in in_progress[:5]:
            lines.append(
                f"• <b><a href=\"{html_escape(issue.issue_url)}\">{html_escape(issue.issue_key)}</a></b>: "
                f"{html_escape(truncate_text(issue.summary, 40))} ({html_escape(issue.assignee or 'Unassigned')})"
            )
    else:
        lines.append("<i>無進行中項目</i>")

    lines.append("")

    # Section 3: In QA / Review
    lines.append(f"🟡 <b>待測試 (In QA / Review - {len(in_qa)} 筆):</b>")
    if in_qa:
        for issue in in_qa[:5]:
            lines.append(
                f"• <b><a href=\"{html_escape(issue.issue_url)}\">{html_escape(issue.issue_key)}</a></b>: "
                f"{html_escape(truncate_text(issue.summary, 40))} ({html_escape(issue.assignee or 'Unassigned')})"
            )
    else:
        lines.append("<i>無待測項目</i>")

    lines.append("")

    # Section 4: Done
    lines.append(f"🟢 <b>本週已完成 (Done - {len(done)} 筆):</b>")
    if done:
        for issue in done[:5]:
            lines.append(
                f"• <b><a href=\"{html_escape(issue.issue_url)}\">{html_escape(issue.issue_key)}</a></b>: "
                f"{html_escape(truncate_text(issue.summary, 40))}"
            )
    else:
        lines.append("<i>無完成項目</i>")

    return "\n".join(lines)

