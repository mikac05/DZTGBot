"""UI handlers package for Telegram draft interaction and inline callback query handling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .callbacks import handle_callback_query
from .drafts import (
    handle_draft_reply_text,
    handle_forward_intake,
    handle_manual_create,
)

if TYPE_CHECKING:
    from dztgbot.services.attachment_service import AttachmentService
    from dztgbot.services.callback_service import CallbackService
    from dztgbot.services.intake_service import IntakeService
    from dztgbot.services.submission_service import SubmissionService
    from dztgbot.services.workflow_service import WorkflowService
    from dztgbot.user_store import UserStore


def build_production_ui_handlers(
    *,
    workflow_service: WorkflowService,
    intake_service: IntakeService,
    callback_service: CallbackService,
    submission_service: SubmissionService | None = None,
    attachment_service: AttachmentService | None = None,
    user_store: UserStore | None = None,
    default_project_key: str = "NGSSA3",
    allowed_user_ids: frozenset[int] | None = None,
) -> Sequence[BaseHandler]:
    """Build bound production handlers using pure application services."""

    from dztgbot.core import ForwardOrReplyToForwardFilter

    async def manual_create_wrapper(update: object, context: object) -> None:
        await handle_manual_create(
            update,  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
            workflow_service=workflow_service,
            callback_service=callback_service,
            default_project_key=default_project_key,
            allowed_user_ids=allowed_user_ids,
        )

    async def forward_intake_wrapper(update: object, context: object) -> None:
        await handle_forward_intake(
            update,  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
            workflow_service=workflow_service,
            intake_service=intake_service,
            callback_service=callback_service,
            default_project_key=default_project_key,
            allowed_user_ids=allowed_user_ids,
        )

    async def draft_reply_text_wrapper(update: object, context: object) -> None:
        await handle_draft_reply_text(
            update,  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
            workflow_service=workflow_service,
            callback_service=callback_service,
            allowed_user_ids=allowed_user_ids,
        )

    async def callback_query_wrapper(update: object, context: object) -> None:
        await handle_callback_query(
            update,  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
            callback_service=callback_service,
            workflow_service=workflow_service,
            submission_service=submission_service,
            attachment_service=attachment_service,
            user_store=user_store,
            allowed_user_ids=allowed_user_ids,
        )

    return (
        CommandHandler("new", manual_create_wrapper),
        CommandHandler("create", manual_create_wrapper),
        MessageHandler(
            ForwardOrReplyToForwardFilter(), forward_intake_wrapper
        ),
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & (~filters.COMMAND),
            draft_reply_text_wrapper,
        ),
        CallbackQueryHandler(
            callback_query_wrapper,
            pattern=r"^j1:",
        ),
    )


__all__ = [
    "build_production_ui_handlers",
    "handle_callback_query",
    "handle_draft_reply_text",
    "handle_forward_intake",
    "handle_manual_create",
]
