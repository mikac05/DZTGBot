"""Async Telegram forward intake."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from telegram import (
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.ext import ContextTypes, MessageHandler, filters

if TYPE_CHECKING:
    from .analysis import GeminiAnalyzer
    from .vpn import NetworkManagerL2tpManager

LOGGER = logging.getLogger(__name__)


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


def build_forward_handler(
    analyzer: "GeminiAnalyzer",
    vpn_manager: "NetworkManagerL2tpManager",
) -> MessageHandler:
    """Build the forward handler with its Gemini dependency injected."""

    async def analyze_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Extract, acknowledge, analyze, and preview an accepted forward."""

        incoming = update.effective_message
        if incoming is None:
            return

        forwarded = forwarded_message_in(incoming)
        if forwarded is None:
            return

        record = extract_forwarded_message(forwarded)
        # TODO: Add an explicit human approval step before any future Jira integration.
        # Do not log record.text, generated descriptions, or credentials.
        LOGGER.info(
            "Accepted forwarded message (media_type=%s, sender_available=%s, chat_available=%s)",
            record.media_type,
            record.original_sender is not None,
            record.original_chat is not None,
        )
        await incoming.reply_text("Forward received. Analyzing...")

        vpn_status = await vpn_manager.status()
        if not vpn_status.is_up:
            await incoming.reply_text(
                "The VPN tunnel is unavailable, so Jira is temporarily unreachable. "
                "I will still prepare the task preview."
            )

        try:
            template = await analyzer.analyze(record)
        except Exception as error:
            # Do not attach third-party tracebacks: provider request URLs may contain
            # sensitive authentication material depending on SDK behavior.
            LOGGER.error("Gemini analysis failed (%s)", type(error).__name__)
            await incoming.reply_text(
                "Gemini analysis failed or returned an invalid result. Please try again later."
            )
            return

        from .analysis import jira_template_preview

        await incoming.reply_text(jira_template_preview(template))

    return MessageHandler(ForwardOrReplyToForwardFilter(), analyze_forward)
