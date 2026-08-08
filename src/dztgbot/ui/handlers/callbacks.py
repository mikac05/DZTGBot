from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.ext import ContextTypes

from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.errors import DomainError
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.policy import (
    DenialCode,
    require_allowed_user,
    user_message_for_denial,
)
from dztgbot.ui.keyboards import (
    build_draft_inline_keyboard,
    build_published_inline_keyboard,
    build_reconcile_inline_keyboard,
    build_retry_inline_keyboard,
    get_remove_reply_keyboard,
)
from dztgbot.ui.rendering import (
    render_cancelled_card,
    render_draft_card,
    render_published_card,
    render_published_update_card,
    render_submission_error_card,
    render_submission_progress,
    render_unknown_outcome_card,
)

if TYPE_CHECKING:
    from dztgbot.services.attachment_service import AttachmentService
    from dztgbot.services.callback_service import CallbackService
    from dztgbot.services.submission_service import SubmissionService
    from dztgbot.services.workflow_service import WorkflowService
    from dztgbot.user_store import UserStore

LOGGER = logging.getLogger(__name__)


async def _get_user_pat(user_store: Any | None, actor_id: int) -> str | None:
    if user_store is None:
        return None
    try:
        if hasattr(user_store, "get"):
            res = user_store.get(actor_id)
            creds = await res if (asyncio.iscoroutine(res) or hasattr(res, "__await__")) else res
            if creds is not None:
                return getattr(creds, "jira_pat", None) or getattr(creds, "pat", None)
        if hasattr(user_store, "get_credentials"):
            res = user_store.get_credentials(actor_id)
            creds = await res if (asyncio.iscoroutine(res) or hasattr(res, "__await__")) else res
            if creds is not None:
                return getattr(creds, "jira_pat", None) or getattr(creds, "pat", None)
    except Exception as exc:
        LOGGER.warning("Credential lookup failed (%s)", type(exc).__name__)
    return None


async def handle_callback_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    callback_service: CallbackService,
    workflow_service: WorkflowService,
    submission_service: SubmissionService | None = None,
    attachment_service: AttachmentService | None = None,
    user_store: UserStore | None = None,
    allowed_user_ids: frozenset[int] | None = None,
) -> None:
    """Authorize and dispatch inline button callback actions."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    actor = update.effective_user
    chat = update.effective_chat
    message = query.message

    if actor is None or chat is None or message is None:
        await query.answer("無效的對話或使用者上下文", show_alert=True)
        return

    if not require_allowed_user(actor.id, allowed_user_ids).allowed:
        await query.answer(
            user_message_for_denial(DenialCode.NOT_ALLOWED_USER), show_alert=True
        )
        return

    # Authorize inbound callback via CallbackService
    auth_result = await callback_service.authorize(
        raw_callback_data=query.data,
        actor_user_id=actor.id,
        chat_id=chat.id,
        chat_type=chat.type,
        message_thread_id=message.message_thread_id,
        preview_message_id=message.message_id,
    )

    if not auth_result.allowed:
        feedback = auth_result.user_message or "操作無效或按鈕已過期"
        await query.answer(feedback, show_alert=True)
        return

    draft = auth_result.draft
    action = auth_result.action
    record = auth_result.record

    if draft is None or action is None or record is None:
        await query.answer("找不到對應的工單草稿記錄", show_alert=True)
        return

    # Standard 5 preview actions
    preview_actions = (
        CallbackAction.CONFIRM,
        CallbackAction.TOGGLE_TYPE,
        CallbackAction.TOGGLE_PRIORITY,
        CallbackAction.EDIT,
        CallbackAction.CANCEL,
    )

    # Dispatch based on authorized action
    if action == CallbackAction.TOGGLE_TYPE:
        await query.answer("類型已更新")
        try:
            updated = await workflow_service.toggle_issue_type(
                draft.draft_id,
                owner_id=actor.id,
                chat_id=chat.id,
                expected_revision=record.expected_revision,
            )
            issued = await callback_service.issue_preview_buttons(
                updated,
                actions=preview_actions,
                preview_message_id=message.message_id,
            )
            await query.edit_message_text(
                render_draft_card(updated),
                parse_mode="HTML",
                reply_markup=build_draft_inline_keyboard(issued),
            )
        except DomainError as error:
            await query.answer(error.classification.safe_code.value, show_alert=True)

    elif action == CallbackAction.TOGGLE_PRIORITY:
        await query.answer("優先級已更新")
        try:
            updated = await workflow_service.toggle_priority(
                draft.draft_id,
                owner_id=actor.id,
                chat_id=chat.id,
                expected_revision=record.expected_revision,
            )
            issued = await callback_service.issue_preview_buttons(
                updated,
                actions=preview_actions,
                preview_message_id=message.message_id,
            )
            await query.edit_message_text(
                render_draft_card(updated),
                parse_mode="HTML",
                reply_markup=build_draft_inline_keyboard(issued),
            )
        except DomainError as error:
            await query.answer(error.classification.safe_code.value, show_alert=True)

    elif action == CallbackAction.CANCEL:
        await query.answer("草稿已取消")
        try:
            cancelled = await workflow_service.cancel_draft(
                draft.draft_id,
                owner_id=actor.id,
                chat_id=chat.id,
                expected_revision=record.expected_revision,
            )
            await callback_service.invalidate_preview_tokens(draft.draft_id)
            await query.edit_message_text(
                render_cancelled_card(cancelled),
                parse_mode="HTML",
            )
        except DomainError as error:
            await query.answer(error.classification.safe_code.value, show_alert=True)

    elif action == CallbackAction.EDIT:
        await query.answer("請在對話視窗中輸入新標題或描述內容", show_alert=True)
        if context.user_data is not None:
            context.user_data["active_draft_id"] = draft.draft_id
            context.user_data["active_draft_revision"] = draft.revision

    elif action == CallbackAction.CONFIRM:
        await query.answer("正在提交工單...")
        pat = await _get_user_pat(user_store, actor.id)

        if not pat:
            await query.answer("請先透過 /auth 設定 Jira PAT 認證憑證", show_alert=True)
            return

        if submission_service is None:
            await query.answer("提交服務尚未配置", show_alert=True)
            return

        await query.edit_message_text(
            render_submission_progress(draft),
            parse_mode="HTML",
        )

        try:
            res = await submission_service.submit(
                draft.draft_id,
                pat=pat,
                expected_revision=record.expected_revision,
            )
        except DomainError as error:
            await query.edit_message_text(
                render_submission_error_card(draft, error.classification.safe_code.value),
                parse_mode="HTML",
            )
            return

        if res.published_issue is not None:
            if attachment_service is not None and draft.attachments:
                try:
                    await attachment_service.upload_pending(draft.draft_id, pat=pat)
                except Exception as exc:
                    LOGGER.warning("Attachment upload failed (%s)", type(exc).__name__)

            await query.edit_message_text(
                render_published_card(res.draft, res.published_issue),
                parse_mode="HTML",
                reply_markup=build_published_inline_keyboard(res.published_issue),
            )

        elif res.draft.state == DraftState.SUBMISSION_RETRYABLE:
            issued = await callback_service.issue_preview_buttons(
                res.draft,
                actions=(CallbackAction.RETRY, CallbackAction.CANCEL),
                preview_message_id=message.message_id,
            )
            await query.edit_message_text(
                render_submission_error_card(res.draft),
                parse_mode="HTML",
                reply_markup=build_retry_inline_keyboard(issued),
            )

        elif res.draft.state == DraftState.SUBMISSION_UNKNOWN:
            issued = await callback_service.issue_preview_buttons(
                res.draft,
                actions=(CallbackAction.RECONCILE, CallbackAction.CANCEL),
                preview_message_id=message.message_id,
            )
            await query.edit_message_text(
                render_unknown_outcome_card(res.draft),
                parse_mode="HTML",
                reply_markup=build_reconcile_inline_keyboard(issued),
            )

    elif action == CallbackAction.RETRY:
        await query.answer("正在重試提交...")
        pat = await _get_user_pat(user_store, actor.id)

        if not pat or submission_service is None:
            await query.answer("無法重試：憑證或服務未配置", show_alert=True)
            return

        try:
            res = await submission_service.submit(
                draft.draft_id,
                pat=pat,
                expected_revision=record.expected_revision,
            )
            if res.published_issue is not None:
                await query.edit_message_text(
                    render_published_card(res.draft, res.published_issue),
                    parse_mode="HTML",
                    reply_markup=build_published_inline_keyboard(res.published_issue),
                )
            else:
                await query.edit_message_text(
                    render_submission_error_card(res.draft),
                    parse_mode="HTML",
                )
        except DomainError as error:
            await query.answer(error.classification.safe_code.value, show_alert=True)

    elif action == CallbackAction.RECONCILE:
        await query.answer("正在調查/調和提交狀態...")
        pat = await _get_user_pat(user_store, actor.id)

        if not pat or submission_service is None:
            await query.answer("無法調和：憑證或服務未配置", show_alert=True)
            return

        try:
            res = await submission_service.reconcile_create(draft.draft_id, pat=pat)
            if res.published_issue is not None:
                await query.edit_message_text(
                    render_published_card(res.draft, res.published_issue),
                    parse_mode="HTML",
                    reply_markup=build_published_inline_keyboard(res.published_issue),
                )
            else:
                await query.answer("調和結果仍不明確，需人工確認 Jira 狀態", show_alert=True)
        except DomainError as error:
            await query.answer(error.classification.safe_code.value, show_alert=True)

    elif action == CallbackAction.EDIT_PUBLISHED:
        await query.answer("準備編輯已發布工單...")
        if submission_service is not None and draft.template is not None:
            try:
                plan = await submission_service.prepare_published_update(
                    draft.draft_id,
                    new_template=draft.template,
                    expected_revision=record.expected_revision,
                )
                await query.edit_message_text(
                    render_published_update_card(plan.draft, plan.changed_fields),
                    parse_mode="HTML",
                )
            except DomainError as error:
                await query.answer(error.classification.safe_code.value, show_alert=True)
