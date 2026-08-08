"""Thin Telegram handlers for draft creation, text forward intake, and text field editing.

Follows strict parse -> pure service -> external I/O -> service -> render workflow.
Enforces private-chat-only access and exact draft/revision context resolution.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.ext import ContextTypes

from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.errors import DomainError
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate
from dztgbot.domain.policy import (
    DenialCode,
    require_allowed_user,
    user_message_for_denial,
)
from dztgbot.ui.keyboards import (
    build_draft_inline_keyboard,
    get_draft_reply_keyboard,
    get_remove_reply_keyboard,
)
from dztgbot.ui.rendering import (
    render_cancelled_card,
    render_draft_card,
    render_private_only_warning,
    render_safe_feedback,
)

if TYPE_CHECKING:
    from dztgbot.services.callback_service import CallbackService
    from dztgbot.services.intake_service import IntakeService
    from dztgbot.services.workflow_service import WorkflowService

LOGGER = logging.getLogger(__name__)

_CURRENT_ALLOWED_USER_IDS: frozenset[int] | None = None

try:
    from dztgbot.config import Settings
    _orig_from_env = Settings.from_environment

    @classmethod
    def _wrapped_from_environment(cls) -> Settings:
        inst = _orig_from_env()
        global _CURRENT_ALLOWED_USER_IDS
        _CURRENT_ALLOWED_USER_IDS = inst.telegram_allowed_user_ids
        return inst

    if not getattr(Settings, "_is_wrapped_by_drafts", False):
        Settings.from_environment = _wrapped_from_environment
        Settings._is_wrapped_by_drafts = True
except Exception:
    pass


def _resolve_allowed_user_ids(
    explicit: frozenset[int] | None,
) -> frozenset[int] | None:
    if explicit is not None:
        return explicit
    return _CURRENT_ALLOWED_USER_IDS


def _is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def handle_forward_intake(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    workflow_service: WorkflowService,
    intake_service: IntakeService,
    callback_service: CallbackService,
    default_project_key: str = "TW",
    allowed_user_ids: frozenset[int] | None = None,
) -> None:
    """Handle inbound forwarded messages or reply-to-forward text intake."""
    if not _is_private_chat(update):
        if update.effective_message:
            await update.effective_message.reply_html(render_private_only_warning())
        return

    actor = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if actor is None or chat is None or message is None:
        return

    effective_allowed = _resolve_allowed_user_ids(allowed_user_ids)
    if not require_allowed_user(actor.id, effective_allowed).allowed:
        await message.reply_html(
            render_safe_feedback(user_message_for_denial(DenialCode.NOT_ALLOWED_USER))
        )
        return

    actor_id = actor.id
    chat_id = chat.id
    raw_text = message.text or message.caption or ""

    # Parse -> service
    template = JiraTaskTemplate(
        project_key=default_project_key,
        issue_type="Task",
        summary=raw_text[:100] if raw_text else "轉傳訊息工單",
        description=raw_text or "轉傳內容內容",
        priority="Medium",
    )

    try:
        draft = await workflow_service.create_manual_draft(
            owner_id=actor_id,
            chat_id=chat_id,
            template=template,
            message_thread_id=message.message_thread_id,
        )
    except DomainError as error:
        await message.reply_html(render_safe_feedback(error.classification.safe_code.value))
        return

    # Render initial message to obtain preview_message_id
    preview_msg = await message.reply_html(
        render_draft_card(draft),
        reply_markup=get_draft_reply_keyboard(),
    )

    # Issue bound callback buttons
    actions = (
        CallbackAction.CONFIRM,
        CallbackAction.TOGGLE_TYPE,
        CallbackAction.TOGGLE_PRIORITY,
        CallbackAction.EDIT,
        CallbackAction.CANCEL,
    )
    issued_buttons = await callback_service.issue_preview_buttons(
        draft,
        actions=actions,
        preview_message_id=preview_msg.message_id,
    )

    # Render final inline keyboard
    await preview_msg.edit_text(
        render_draft_card(draft),
        parse_mode="HTML",
        reply_markup=build_draft_inline_keyboard(issued_buttons),
    )

    # Track exact draft/revision context
    if context.user_data is not None:
        context.user_data["active_draft_id"] = draft.draft_id
        context.user_data["active_draft_revision"] = draft.revision


async def handle_manual_create(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    workflow_service: WorkflowService,
    callback_service: CallbackService,
    default_project_key: str = "TW",
    allowed_user_ids: frozenset[int] | None = None,
) -> None:
    """Handle /create or manual draft initialization."""
    if not _is_private_chat(update):
        if update.effective_message:
            await update.effective_message.reply_html(render_private_only_warning())
        return

    actor = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if actor is None or chat is None or message is None:
        return

    effective_allowed = _resolve_allowed_user_ids(allowed_user_ids)
    if not require_allowed_user(actor.id, effective_allowed).allowed:
        await message.reply_html(
            render_safe_feedback(user_message_for_denial(DenialCode.NOT_ALLOWED_USER))
        )
        return

    actor_id = actor.id
    chat_id = chat.id

    # Parse argument text if present
    summary_text = "新工單草稿"
    if context.args:
        summary_text = " ".join(context.args)

    template = JiraTaskTemplate(
        project_key=default_project_key,
        issue_type="Task",
        summary=summary_text,
        description="由手動指令建立",
        priority="Medium",
    )

    try:
        draft = await workflow_service.create_manual_draft(
            owner_id=actor_id,
            chat_id=chat_id,
            template=template,
            message_thread_id=message.message_thread_id,
        )
    except DomainError as error:
        await message.reply_html(render_safe_feedback(error.classification.safe_code.value))
        return

    preview_msg = await message.reply_html(
        render_draft_card(draft),
        reply_markup=get_draft_reply_keyboard(),
    )

    actions = (
        CallbackAction.CONFIRM,
        CallbackAction.TOGGLE_TYPE,
        CallbackAction.TOGGLE_PRIORITY,
        CallbackAction.EDIT,
        CallbackAction.CANCEL,
    )
    issued_buttons = await callback_service.issue_preview_buttons(
        draft,
        actions=actions,
        preview_message_id=preview_msg.message_id,
    )

    await preview_msg.edit_text(
        render_draft_card(draft),
        parse_mode="HTML",
        reply_markup=build_draft_inline_keyboard(issued_buttons),
    )

    if context.user_data is not None:
        context.user_data["active_draft_id"] = draft.draft_id
        context.user_data["active_draft_revision"] = draft.revision


async def handle_draft_reply_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    workflow_service: WorkflowService,
    callback_service: CallbackService,
    allowed_user_ids: frozenset[int] | None = None,
) -> None:
    """Handle text input sent during draft review/editing or reply keyboard clicks."""
    if not _is_private_chat(update):
        if update.effective_message:
            await update.effective_message.reply_html(render_private_only_warning())
        return

    actor = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if actor is None or chat is None or message is None or not message.text:
        return

    effective_allowed = _resolve_allowed_user_ids(allowed_user_ids)
    if not require_allowed_user(actor.id, effective_allowed).allowed:
        await message.reply_html(
            render_safe_feedback(user_message_for_denial(DenialCode.NOT_ALLOWED_USER))
        )
        return

    actor_id = actor.id
    chat_id = chat.id
    text = message.text.strip()

    user_data = context.user_data or {}
    draft_id = user_data.get("active_draft_id")
    expected_revision = user_data.get("active_draft_revision")

    if not draft_id:
        # No active draft tracking
        return

    try:
        draft = await workflow_service.get_draft(
            draft_id, actor_id=actor_id, chat_id=chat_id
        )
    except DomainError:
        user_data.pop("active_draft_id", None)
        user_data.pop("active_draft_revision", None)
        await message.reply_html(render_safe_feedback("找不到活躍的工單草稿或無權存取。"))
        return

    if expected_revision is not None and draft.revision != expected_revision:
        await message.reply_html(
            render_safe_feedback("草稿版本已變更，請使用最新按鈕操作。")
        )
        return

    # Process text input commands / reply keyboard options
    if text == "❌ 取消草稿":
        try:
            cancelled = await workflow_service.cancel_draft(
                draft.draft_id,
                owner_id=actor_id,
                chat_id=chat_id,
                expected_revision=draft.revision,
            )
            await callback_service.invalidate_preview_tokens(draft.draft_id)
            user_data.pop("active_draft_id", None)
            user_data.pop("active_draft_revision", None)
            await message.reply_html(
                render_cancelled_card(cancelled),
                reply_markup=get_remove_reply_keyboard(),
            )
        except DomainError as error:
            await message.reply_html(
                render_safe_feedback(error.classification.safe_code.value)
            )
        return

    if text.startswith("🏷️ 類型:"):
        try:
            updated = await workflow_service.toggle_issue_type(
                draft.draft_id,
                owner_id=actor_id,
                chat_id=chat_id,
                expected_revision=draft.revision,
            )
            user_data["active_draft_revision"] = updated.revision
            await message.reply_html(
                render_draft_card(updated),
                reply_markup=get_draft_reply_keyboard(),
            )
        except DomainError as error:
            await message.reply_html(
                render_safe_feedback(error.classification.safe_code.value)
            )
        return

    if text.startswith("⚡ 優先級:"):
        try:
            updated = await workflow_service.toggle_priority(
                draft.draft_id,
                owner_id=actor_id,
                chat_id=chat_id,
                expected_revision=draft.revision,
            )
            user_data["active_draft_revision"] = updated.revision
            await message.reply_html(
                render_draft_card(updated),
                reply_markup=get_draft_reply_keyboard(),
            )
        except DomainError as error:
            await message.reply_html(
                render_safe_feedback(error.classification.safe_code.value)
            )
        return

    # Freeform edit text (updating summary/description)
    if draft.template is not None:
        new_template = replace(draft.template, summary=text)
        try:
            updated = await workflow_service.update_template(
                draft.draft_id,
                owner_id=actor_id,
                chat_id=chat_id,
                new_template=new_template,
                expected_revision=draft.revision,
            )
            user_data["active_draft_revision"] = updated.revision
            await message.reply_html(
                render_draft_card(updated, title="<b>✏️ 已更新 Summary</b>"),
                reply_markup=get_draft_reply_keyboard(),
            )
        except DomainError as error:
            await message.reply_html(
                render_safe_feedback(error.classification.safe_code.value)
            )
