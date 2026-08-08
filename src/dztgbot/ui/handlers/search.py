"""Search and URL unfurl handlers for Jira 8.4.1 issues.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import ContextTypes

from dztgbot.domain.models import JiraSearchResult
from dztgbot.services.card_tracker_service import CardTrackerService
from dztgbot.services.jira_issue_service import JiraIssueService
from dztgbot.ui.keyboards import (
    build_paginated_search_keyboard,
    build_universal_issue_card_keyboard,
    extract_figma_url,
)
from dztgbot.ui.rendering import (
    render_compact_search_list,
    render_issue_card,
    render_safe_feedback,
    render_standup_report,
)


logger = logging.getLogger(__name__)

# Pattern for matching Jira issue URLs, e.g. https://jira.example.com/browse/PROJ-123
JIRA_URL_PATTERN = re.compile(r"https?://[^/]+/browse/([A-Z0-9]+-\d+)", re.IGNORECASE)
ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z0-9]+-\d+)\b")


class SearchHandlers:
    """Handlers for search commands, filter bars, URL unfurling, and inline queries."""

    def __init__(
        self,
        jira_issue_service: JiraIssueService,
        card_tracker_service: CardTrackerService,
    ) -> None:
        self._issue_service = jira_issue_service
        self._card_tracker = card_tracker_service

    async def _send_search_results(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        title: str,
        fetcher: Callable[[int], Awaitable[JiraSearchResult]],
        page: int = 1,
    ) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return

        try:
            result = await fetcher(user.id)
        except Exception as error:
            logger.warning("Search failed for user %s (%s): %s", user.id, title, error)
            feedback = render_safe_feedback(str(error))
            if update.message:
                await update.message.reply_html(feedback)
            elif update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_html(feedback)
            return

        filter_code = "my"
        if "Created" in title:
            filter_code = "created"
        elif "Unassigned" in title:
            filter_code = "unassigned"
        elif "Blocked" in title:
            filter_code = "blocked"
        elif "Sprint" in title:
            filter_code = "sprint"

        if context.user_data is not None:
            context.user_data["last_search_result"] = result
            context.user_data["last_search_title"] = title
            context.user_data["last_search_filter"] = filter_code

        list_html = render_compact_search_list(result, title, page=page)
        keyboard = build_paginated_search_keyboard(result.issues, filter_code, page=page)

        if update.callback_query and update.callback_query.message:
            await update.callback_query.answer()
            await update.callback_query.message.reply_html(list_html, reply_markup=keyboard)
        elif update.message:
            await update.message.reply_html(list_html, reply_markup=keyboard)

    async def handle_show_card_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if not query or not query.data or not user or not chat:
            return

        await query.answer()
        issue_key = query.data.split("shc:")[1] if "shc:" in query.data else ""
        if not issue_key:
            return

        try:
            issue = await self._issue_service.get_issue(user.id, issue_key)
            transitions = await self._issue_service.get_transitions(user.id, issue_key)
            primary_trans = transitions[0] if transitions else None

            card_html = render_issue_card(issue)
            keyboard = build_universal_issue_card_keyboard(
                issue_key=issue.issue_key,
                issue_url=issue.issue_url,
                is_blocked=issue.is_flagged or bool(issue.blocker_keys),
                is_watching=issue.is_watching,
                primary_transition=primary_trans,
                figma_url=extract_figma_url(issue.description),
            )
            msg = await chat.send_message(card_html, parse_mode="HTML", reply_markup=keyboard)
            await self._card_tracker.register_card(chat.id, msg.message_id, issue.issue_key, user.id)
        except Exception as error:
            logger.warning("Show card failed for %s: %s", issue_key, error)
            await query.message.reply_html(render_safe_feedback(str(error)))

    async def handle_page_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not query.data or not user:
            return

        await query.answer()
        parts = query.data.split(":")
        if len(parts) < 3:
            return
        filter_code = parts[1]
        try:
            page = int(parts[2])
        except ValueError:
            page = 1

        result: JiraSearchResult | None = None
        title = "Search Results"
        if context.user_data is not None and "last_search_result" in context.user_data:
            result = context.user_data["last_search_result"]
            title = context.user_data.get("last_search_title", title)

        if result is None:
            if filter_code == "my":
                await self.handle_my_open(update, context)
            elif filter_code == "created":
                await self.handle_created(update, context)
            elif filter_code == "unassigned":
                await self.handle_unassigned(update, context)
            elif filter_code == "blocked":
                await self.handle_blocked(update, context)
            elif filter_code == "sprint":
                await self.handle_sprint(update, context)
            return

        list_html = render_compact_search_list(result, title, page=page)
        keyboard = build_paginated_search_keyboard(result.issues, filter_code, page=page)
        if query.message:
            await query.message.edit_text(list_html, parse_mode="HTML", reply_markup=keyboard)

    async def handle_my_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_search_results(update, context, "My Open Issues", self._issue_service.search_my_open)

    async def handle_created(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_search_results(update, context, "Issues I Created", self._issue_service.search_i_created)

    async def handle_unassigned(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_search_results(update, context, "Unassigned Open Issues", self._issue_service.search_unassigned)

    async def handle_blocked(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_search_results(update, context, "Blocked & Flagged Issues", self._issue_service.search_blocked)

    async def handle_sprint(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_search_results(update, context, "Active Sprint Issues", self._issue_service.search_sprint)

    async def handle_keyword_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            if update.message:
                await update.message.reply_html("Usage: <code>/s &lt;keywords&gt;</code>")
            return
        keywords = " ".join(context.args)

        async def fetcher(uid: int) -> JiraSearchResult:
            return await self._issue_service.search_keywords(uid, keywords)

        await self._send_search_results(update, context, f"Search: {keywords}", fetcher)

    async def handle_search_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_html(
                "🔍 <b>Jira 工單搜尋</b>\n\n"
                "請使用 <code>/s &lt;關鍵字&gt;</code> 搜尋工單 (例如: <code>/s 登入</code>)。\n"
                "或點擊快速選單: <b>[📋 指派給我的]</b> 或 <b>[🚩 我建的]</b>"
            )

    async def handle_standup_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user:
            return

        try:
            blocked, in_progress, in_qa, done = await self._issue_service.get_standup_summary(user.id)
            report_html = render_standup_report(blocked, in_progress, in_qa, done)
            if update.message:
                await update.message.reply_html(report_html)
            elif update.callback_query and update.callback_query.message:
                await update.callback_query.answer()
                await update.callback_query.message.reply_html(report_html)
        except Exception as error:
            logger.warning("Standup report failed for user %s: %s", user.id, error)
            if update.message:
                await update.message.reply_html(render_safe_feedback(str(error)))

    async def handle_filter_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        code = query.data.removeprefix("flt:")
        if code == "my":
            await self.handle_my_open(update, context)
        elif code == "created":
            await self.handle_created(update, context)
        elif code == "unassigned":
            await self.handle_unassigned(update, context)
        elif code == "blocked":
            await self.handle_blocked(update, context)
        elif code == "sprint":
            await self.handle_sprint(update, context)

    async def handle_url_unfurl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        message = update.message
        if not user or not chat or not message or not message.text:
            return

        match = JIRA_URL_PATTERN.search(message.text)
        if not match:
            return

        issue_key = match.group(1).upper()
        try:
            issue = await self._issue_service.get_issue(user.id, issue_key)
            card_html = render_issue_card(issue)
            keyboard = build_universal_issue_card_keyboard(
                issue_key=issue.issue_key,
                issue_url=issue.issue_url,
                is_blocked=issue.is_flagged or bool(issue.blocker_keys),
            )
            msg = await message.reply_html(card_html, reply_markup=keyboard)
            await self._card_tracker.register_card(chat.id, msg.message_id, issue.issue_key, user.id)
        except Exception as error:
            logger.debug("URL unfurl skipped/failed for %s: %s", issue_key, error)

    async def handle_inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        inline_query = update.inline_query
        user = update.effective_user
        if not inline_query or not user:
            return

        query_text = inline_query.query.strip()
        if not query_text:
            return

        try:
            result = await self._issue_service.search_keywords(user.id, query_text, max_results=5)
            articles = []
            for issue in result.issues:
                card_html = render_issue_card(issue)
                keyboard = build_universal_issue_card_keyboard(
                    issue_key=issue.issue_key,
                    issue_url=issue.issue_url,
                    is_blocked=issue.is_flagged or bool(issue.blocker_keys),
                )
                articles.append(
                    InlineQueryResultArticle(
                        id=f"jira_{issue.issue_key}",
                        title=f"{issue.issue_key}: {issue.summary}",
                        description=f"{issue.status} | {issue.priority} | {issue.assignee or 'Unassigned'}",
                        input_message_content=InputTextMessageContent(card_html, parse_mode="HTML"),
                        reply_markup=keyboard,
                    )
                )
            await inline_query.answer(articles, cache_time=10)
        except Exception as error:
            logger.warning("Inline query search failed for user %s: %s", user.id, error)
