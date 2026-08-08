"""Stateless compatibility facades and Telegram message normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Sequence

from telegram import (
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from telegram.ext import BaseHandler, filters

if TYPE_CHECKING:
    from .analysis import GeminiAnalyzer
    from .jira_client import JiraClient
    from .user_store import UserStore
    from .vpn import NetworkManagerL2tpManager

LOGGER = logging.getLogger(__name__)

# Maximum number of forwarded messages that can be batched into a single Jira issue
MAX_BATCH_SIZE = 20
# Editing draft timeout in seconds (15 minutes)
EDITING_TIMEOUT_SECONDS = 900


class MediaType(StrEnum):
    TEXT = "text"
    ANIMATION = "animation"
    AUDIO = "audio"
    CONTACT = "contact"
    DICE = "dice"
    DOCUMENT = "document"
    GAME = "game"
    PAID_MEDIA = "paid_media"
    PHOTO = "photo"
    POLL = "poll"
    STICKER = "sticker"
    STORY = "story"
    VENUE = "venue"
    LOCATION = "location"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    VOICE = "voice"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    """A Telegram user or chat identity, including partial/hidden identities."""

    id: int | None
    display_name: str | None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class ForwardedMessage:
    """Normalized fields extracted from a forwarded Telegram message."""

    original_sender: TelegramIdentity | None
    original_chat: TelegramIdentity | None
    text: str | None
    media_type: MediaType


def forwarded_message_in(message: Message) -> Message | None:
    """Return the forwarded message itself or the forwarded message being replied to."""

    if message.forward_origin is not None:
        return message

    replied_to = message.reply_to_message
    if replied_to is not None and replied_to.forward_origin is not None:
        return replied_to

    return None


class ForwardOrReplyToForwardFilter(filters.MessageFilter):
    """Accept direct forwards and messages replying directly to a forward."""

    def filter(self, message: Message) -> bool:
        return forwarded_message_in(message) is not None


def _chat_identity(chat: object) -> TelegramIdentity:
    return TelegramIdentity(
        id=getattr(chat, "id", None),
        display_name=getattr(chat, "title", None) or getattr(chat, "full_name", None),
        username=getattr(chat, "username", None),
    )


def _origin_identities(message: Message) -> tuple[TelegramIdentity | None, TelegramIdentity | None]:
    origin = message.forward_origin

    if isinstance(origin, MessageOriginUser):
        user = origin.sender_user
        sender = TelegramIdentity(
            id=user.id,
            display_name=user.full_name,
            username=user.username,
        )
        return sender, None

    if isinstance(origin, MessageOriginHiddenUser):
        return TelegramIdentity(id=None, display_name=origin.sender_user_name), None

    if isinstance(origin, MessageOriginChat):
        sender = (
            TelegramIdentity(id=None, display_name=origin.author_signature)
            if origin.author_signature
            else None
        )
        return sender, _chat_identity(origin.sender_chat)

    if isinstance(origin, MessageOriginChannel):
        sender = (
            TelegramIdentity(id=None, display_name=origin.author_signature)
            if origin.author_signature
            else None
        )
        return sender, _chat_identity(origin.chat)

    return None, None


def _media_type(message: Message) -> MediaType:
    if message.text is not None:
        return MediaType.TEXT

    media_fields = (
        (MediaType.ANIMATION, "animation"),
        (MediaType.AUDIO, "audio"),
        (MediaType.CONTACT, "contact"),
        (MediaType.DICE, "dice"),
        (MediaType.DOCUMENT, "document"),
        (MediaType.GAME, "game"),
        (MediaType.PAID_MEDIA, "paid_media"),
        (MediaType.PHOTO, "photo"),
        (MediaType.POLL, "poll"),
        (MediaType.STICKER, "sticker"),
        (MediaType.STORY, "story"),
        (MediaType.VENUE, "venue"),
        (MediaType.LOCATION, "location"),
        (MediaType.VIDEO, "video"),
        (MediaType.VIDEO_NOTE, "video_note"),
        (MediaType.VOICE, "voice"),
    )
    for media_type, field_name in media_fields:
        if getattr(message, field_name, None):
            return media_type

    return MediaType.UNKNOWN


def extract_forwarded_message(message: Message) -> ForwardedMessage:
    """Normalize a message already known to be a forward."""

    if message.forward_origin is None:
        raise ValueError("message is not a forward")

    original_sender, original_chat = _origin_identities(message)
    return ForwardedMessage(
        original_sender=original_sender,
        original_chat=original_chat,
        text=message.text if message.text is not None else message.caption,
        media_type=_media_type(message),
    )


async def get_main_menu_keyboard(
    user_id: int | None, user_store: "UserStore"
) -> object:
    """Return dynamic main menu keyboard."""
    from .jira_auth import get_main_menu_keyboard as auth_get_keyboard
    return await auth_get_keyboard(user_id, user_store)


def get_draft_keyboard() -> object:
    """Return draft reply keyboard."""
    from .ui.keyboards import get_draft_reply_keyboard
    return get_draft_reply_keyboard()


def build_forward_handlers(
    analyzer: object = None,
    vpn_manager: object = None,
    user_store: object = None,
    jira_client: object = None,
) -> Sequence[BaseHandler]:
    """Compatibility facade delegating to pure services and UI handlers."""
    from .infrastructure import (
        AsyncTaskScheduler,
        SystemClock,
        UuidIdGenerator,
    )
    from .services import (
        CallbackService,
        IntakeService,
        WorkflowService,
    )
    from .ui.handlers import build_production_ui_handlers

    if hasattr(analyzer, "create_manual_draft") or hasattr(analyzer, "collect_message"):
        workflow_svc = analyzer
        callback_svc = getattr(vpn_manager, "callback_service", None) or CallbackService(
            drafts=getattr(workflow_svc, "_repository", None),  # type: ignore
            tokens=getattr(workflow_svc, "_repository", None),  # type: ignore
        )
        intake_svc = getattr(vpn_manager, "intake_service", None)
        return build_production_ui_handlers(
            workflow_service=workflow_svc,  # type: ignore
            intake_service=intake_svc,  # type: ignore
            callback_service=callback_svc,
            user_store=user_store,  # type: ignore
        )

    dummy_clock = SystemClock()
    dummy_id = UuidIdGenerator()
    dummy_sched = AsyncTaskScheduler()

    ws = WorkflowService(repository=None, clock=dummy_clock, id_generator=dummy_id)  # type: ignore
    cs = CallbackService(drafts=None, tokens=None, clock=dummy_clock)  # type: ignore
    intake = IntakeService(
        repository=None,  # type: ignore
        analyzer=analyzer if hasattr(analyzer, "analyze_messages") else None,  # type: ignore
        rules_repository=None,  # type: ignore
        scheduler=dummy_sched,
        clock=dummy_clock,
        id_generator=dummy_id,
        default_project_key="NGSSA3",
    )

    return build_production_ui_handlers(
        workflow_service=ws,
        intake_service=intake,
        callback_service=cs,
    )


__all__ = [
    "EDITING_TIMEOUT_SECONDS",
    "ForwardOrReplyToForwardFilter",
    "ForwardedMessage",
    "MAX_BATCH_SIZE",
    "MediaType",
    "TelegramIdentity",
    "build_forward_handlers",
    "extract_forwarded_message",
    "forwarded_message_in",
    "get_draft_keyboard",
    "get_main_menu_keyboard",
]
