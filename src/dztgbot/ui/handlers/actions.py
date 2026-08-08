"""Interactive action handlers for Jira 8.4.1 Issue Cards.

Handles Move (transitions), Assign, Block/Unblock, Reply-to-Card Comments/Attachments, and Sub-task drafts.
"""

from __future__ import annotations

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from dztgbot.services.jira_issue_service import JiraIssueService
from dztgbot.services.card_tracker_service import CardTrackerService
from dztgbot.services.workflow_service import WorkflowService
from dztgbot.ui.rendering import render_issue_card, render_safe_feedback
from dztgbot.ui.keyboards import (
    build_universal_issue_card_keyboard,
    extract_figma_url,
)
from dztgbot.domain.models import JiraIssueView, JiraTaskTemplate


logger = logging.getLogger(__name__)


class ActionHandlers:
    """Handles inline card actions (Move, Assign, Block, Comment reply, Sub-task)."""

    def __init__(
        self,
        jira_issue_service: JiraIssueService,
        card_tracker_service: CardTrackerService,
        workflow_service: WorkflowService,
    ) -> None:
        self._issue_service = jira_issue_service
        self._card_tracker = card_tracker_service
        self._workflow_service = workflow_service

    async def _update_card_in_place(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, issue: JiraIssueView) -> None:
        card_html = render_issue_card(issue)
        keyboard = build_universal_issue_card_keyboard(
            issue_key=issue.issue_key,
            issue_url=issue.issue_url,
            is_blocked=issue.is_flagged or bool(issue.blocker_keys),
            is_watching=issue.is_watching,
            figma_url=extract_figma_url(issue.description),
        )
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=card_html,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.debug("In-place edit message chat_id=%s message_id=%s failed: %s", chat_id, message_id, error)

    async def refresh_all_cards(self, context: ContextTypes.DEFAULT_TYPE, issue: JiraIssueView) -> None:
        async def updater(c_id: int, m_id: int, iss: JiraIssueView) -> None:
            await self._update_card_in_place(context, c_id, m_id, iss)

        await self._card_tracker.refresh_issue_cards(issue, updater)

    async def handle_card_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not query.data or not user:
            return

        data = query.data
        if ":" not in data:
            return

        action_prefix, issue_key = data.split(":", 1)
        await query.answer()

        if action_prefix == "card_mv":
            await self._handle_move_start(update, context, user.id, issue_key)
        elif action_prefix == "card_asn":
            await self._handle_assign_start(update, context, user.id, issue_key)
        elif action_prefix == "card_blk":
            await self._handle_block_start(update, context, user.id, issue_key)
        elif action_prefix == "card_ubk":
            await self._handle_unblock_start(update, context, user.id, issue_key)
        elif action_prefix == "card_wtc":
            await self._handle_watch_toggle(update, context, user.id, issue_key, watch=True)
        elif action_prefix == "card_uwtc":
            await self._handle_watch_toggle(update, context, user.id, issue_key, watch=False)
        elif action_prefix == "card_cmt":
            await query.message.reply_html(
                f"💬 <b>新增對 {issue_key} 的留言:</b>\n"
                f"請直接<b>回覆 (Reply)</b> 此 Issue Card 訊息輸入文字或照片。",
            )
        elif action_prefix == "card_sub":
            await self._handle_subtask_start(update, context, user.id, issue_key)

    async def _handle_watch_toggle(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, issue_key: str, watch: bool
    ) -> None:
        try:
            if watch:
                updated_issue = await self._issue_service.watch_issue(user_id, issue_key)
                if update.callback_query:
                    await update.callback_query.answer("👁️ 已加入關注此工單！", show_alert=False)
            else:
                updated_issue = await self._issue_service.unwatch_issue(user_id, issue_key)
                if update.callback_query:
                    await update.callback_query.answer("👁️ 已取消關注此工單。", show_alert=False)

            await self.refresh_all_cards(context, updated_issue)
        except Exception as error:
            logger.warning("Watch toggle failed for %s: %s", issue_key, error)
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_html(render_safe_feedback(str(error)))

    async def _handle_move_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, issue_key: str) -> None:
        try:
            transitions = await self._issue_service.get_transitions(user_id, issue_key)
            if not transitions:
                if update.callback_query and update.callback_query.message:
                    await update.callback_query.message.reply_html(f"⚠️ {issue_key} 無可用的狀態轉換。")
                return

            buttons: list[list[InlineKeyboardButton]] = []
            for t in transitions:
                buttons.append([
                    InlineKeyboardButton(
                        f"➡️ {t.name} ({t.to_status})",
                        callback_data=f"do_mv:{issue_key}:{t.transition_id}",
                    )
                ])
            markup = InlineKeyboardMarkup(buttons)
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_html(
                    f"➡️ <b>請選擇 {issue_key} 要轉移的狀態:</b>",
                    reply_markup=markup,
                )
        except Exception as error:
            logger.warning("Failed to get transitions for %s: %s", issue_key, error)
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_html(render_safe_feedback(str(error)))

    async def handle_execute_move(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not query.data or not user:
            return

        parts = query.data.split(":")
        if len(parts) != 3:
            return

        issue_key, transition_id = parts[1], parts[2]
        await query.answer("狀態更新中...")

        try:
            updated_issue = await self._issue_service.execute_transition(user.id, issue_key, transition_id)
            await self.refresh_all_cards(context, updated_issue)
            if query.message:
                await query.message.reply_html(f"✅ <b>{issue_key}</b> 狀態已成功更新為 <b>{updated_issue.status}</b>！")
        except Exception as error:
            logger.warning("Execute transition failed for %s: %s", issue_key, error)
            if query.message:
                await query.message.reply_html(render_safe_feedback(str(error)))

    async def _handle_assign_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, issue_key: str) -> None:
        if not update.callback_query or not update.callback_query.message:
            return

        username = update.effective_user.username or update.effective_user.first_name if update.effective_user else ""
        buttons = [
            [
                InlineKeyboardButton("👤 指派給我 (Me)", callback_data=f"do_asn:{issue_key}:{username}"),
                InlineKeyboardButton("🚫 解除指派 (Unassign)", callback_data=f"do_asn:{issue_key}:UNASSIGNED"),
            ]
        ]
        markup = InlineKeyboardMarkup(buttons)
        await update.callback_query.message.reply_html(
            f"👤 <b>請選擇 {issue_key} 的指派對象:</b>",
            reply_markup=markup,
        )

    async def handle_execute_assign(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not query.data or not user:
            return

        parts = query.data.split(":")
        if len(parts) != 3:
            return

        issue_key, assignee = parts[1], parts[2]
        target_assignee = "" if assignee == "UNASSIGNED" else assignee
        await query.answer("更新經辦人...")

        try:
            updated_issue = await self._issue_service.assign_issue(user.id, issue_key, target_assignee)
            await self.refresh_all_cards(context, updated_issue)
            if query.message:
                assigned_text = updated_issue.assignee or "Unassigned"
                await query.message.reply_html(f"✅ <b>{issue_key}</b> 經辦人已更新為 <b>{assigned_text}</b>！")
        except Exception as error:
            logger.warning("Execute assign failed for %s: %s", issue_key, error)
            if query.message:
                await query.message.reply_html(render_safe_feedback(str(error)))

    async def _handle_block_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, issue_key: str) -> None:
        if not update.callback_query or not update.callback_query.message:
            return
        context.user_data["pending_block_issue"] = issue_key
        await update.callback_query.message.reply_html(
            f"⚠️ <b>標記阻礙 (Block) {issue_key}:</b>\n"
            f"請直接發送訊息輸入<b>阻礙此工單的 Jira Issue Key (例如: PROJ-100)</b>，"
            f"亦可附帶說明原因 (例如: <code>PROJ-100 等待 API 格式確認</code>)。"
        )

    async def handle_block_input_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.message
        if not user or not message or not message.text:
            return

        issue_key = context.user_data.pop("pending_block_issue", None)
        if not issue_key:
            return

        text = message.text.strip()
        parts = text.split(maxsplit=1)
        blocker_key = parts[0].upper()
        reason = parts[1] if len(parts) > 1 else None

        try:
            updated_issue = await self._issue_service.block_issue(user.id, issue_key, blocker_key, reason=reason)
            await self.refresh_all_cards(context, updated_issue)
            await message.reply_html(f"⚠️ <b>{issue_key}</b> 已成功新增阻礙關聯: <b>{blocker_key}</b>！")
        except Exception as error:
            logger.warning("Block issue failed for %s: %s", issue_key, error)
            await message.reply_html(render_safe_feedback(str(error)))

    async def _handle_unblock_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, issue_key: str) -> None:
        try:
            issue = await self._issue_service.get_issue(user_id, issue_key)
            if not issue.blocker_keys:
                if update.callback_query and update.callback_query.message:
                    await update.callback_query.message.reply_html(f"ℹ️ {issue_key} 目前無記錄中的阻礙關聯。")
                return

            buttons = []
            for b_key in issue.blocker_keys:
                buttons.append([
                    InlineKeyboardButton(
                        f"🗑️ 解除與 {b_key} 的阻礙關聯",
                        callback_data=f"do_ubk:{issue_key}:{b_key}",
                    )
                ])
            markup = InlineKeyboardMarkup(buttons)
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_html(
                    f"✅ <b>請選擇要為 {issue_key} 解除的阻礙工單:</b>",
                    reply_markup=markup,
                )
        except Exception as error:
            logger.warning("Unblock start failed for %s: %s", issue_key, error)
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_html(render_safe_feedback(str(error)))

    async def handle_execute_unblock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not query.data or not user:
            return

        parts = query.data.split(":")
        if len(parts) != 3:
            return

        issue_key, blocker_key = parts[1], parts[2]
        await query.answer("解除阻礙中...")

        try:
            # Unblock uses issue link ID or key
            updated_issue = await self._issue_service.unblock_issue(user.id, issue_key, blocker_key)
            await self.refresh_all_cards(context, updated_issue)
            if query.message:
                await query.message.reply_html(f"✅ <b>{issue_key}</b> 已解除與 <b>{blocker_key}</b> 的阻礙關聯！")
        except Exception as error:
            logger.warning("Execute unblock failed for %s: %s", issue_key, error)
            if query.message:
                await query.message.reply_html(render_safe_feedback(str(error)))

    async def handle_reply_comment_or_attachment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        user = update.effective_user
        if not message or not user or not message.reply_to_message:
            return

        # Check if the message replied to is an Issue Card or has an issue key
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        import re
        match = re.search(r"\b([A-Z0-9]+-\d+)\b", reply_text)
        if not match:
            return

        issue_key = match.group(1).upper()

        try:
            if message.photo:
                photo = message.photo[-1]
                file_obj = await photo.get_file()
                content = bytes(await file_obj.download_as_bytearray())
                updated_issue = await self._issue_service.upload_attachment(
                    user.id, issue_key, f"photo_{photo.file_unique_id}.jpg", content, "image/jpeg"
                )
                await message.reply_html(f"✅ 照片已成功作為附件上傳至 Jira 工單 <b>{issue_key}</b>！")
            elif message.text:
                comment_text = message.text.strip()
                updated_issue = await self._issue_service.add_comment(user.id, issue_key, comment_text)
                await message.reply_html(f"✅ 留言已成功新增至 Jira 工單 <b>{issue_key}</b>！")
            else:
                return

            await self.refresh_all_cards(context, updated_issue)
        except Exception as error:
            logger.warning("Reply comment/attachment failed for %s: %s", issue_key, error)
            await message.reply_html(render_safe_feedback(str(error)))

    async def _handle_subtask_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, parent_issue_key: str) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else user_id
        project_key = parent_issue_key.split("-")[0]
        template = JiraTaskTemplate(
            project_key=project_key,
            issue_type="Sub-task",
            summary=f"Sub-task for {parent_issue_key}",
            description=f"Parent: {parent_issue_key}",
            priority="Medium",
        )
        draft = await self._workflow_service.create_manual_draft(
            owner_id=user_id,
            chat_id=chat_id,
            template=template,
        )
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_html(
                f"➕ <b>已建立子任務 (Sub-task) 草稿！</b>\n"
                f"<b>Parent Key:</b> <code>{parent_issue_key}</code>\n"
                f"<b>Draft ID:</b> <code>{draft.draft_id}</code>\n\n"
                f"請輸入子任務的標題與敘述完成草稿。"
            )
