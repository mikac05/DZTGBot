"""Async Telegram forward intake."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .vpn import VpnState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .analysis import GeminiAnalyzer
    from .jira_client import JiraClient
    from .user_store import UserStore
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


def build_forward_handlers(
    analyzer: "GeminiAnalyzer",
    vpn_manager: "NetworkManagerL2tpManager",
    user_store: "UserStore",
    jira_client: "JiraClient",
) -> "Sequence[BaseHandler]":
    """Build the forward analysis handler and issue-confirmation callback."""

    async def analyze_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Extract, analyze, preview, and offer one-tap issue creation."""

        incoming = update.effective_message
        user = update.effective_user
        if incoming is None or user is None:
            return

        forwarded = forwarded_message_in(incoming)
        if forwarded is None:
            return

        record = extract_forwarded_message(forwarded)
        LOGGER.info(
            "Accepted forwarded message (media_type=%s, sender_available=%s, chat_available=%s)",
            record.media_type,
            record.original_sender is not None,
            record.original_chat is not None,
        )
        await incoming.reply_text("\U0001f4e8 Forward received. Analyzing...")

        try:
            template = await analyzer.analyze(record)
        except Exception as error:
            LOGGER.error("Gemini analysis failed (%s: %s)", type(error).__name__, error)
            await incoming.reply_text(
                "\u274c Gemini analysis failed or returned an invalid result. "
                "Please check GEMINI_MODEL / GEMINI_API_KEY."
            )
            return

        from .analysis import jira_template_preview

        preview = jira_template_preview(template)

        # Store the template so the callback can retrieve it.
        if context.user_data is not None:
            context.user_data["pending_template"] = template

        # Check whether the user has stored Jira credentials.
        credentials = await user_store.get(user.id)
        if credentials is None:
            await incoming.reply_text(
                f"{preview}\n\n"
                "\u26a0\ufe0f You haven't connected your Jira account yet. "
                "Use /auth to connect, then forward the message again."
            )
            return

        # Warn about VPN problems (skip if VPN is intentionally disabled).
        vpn_warning = ""
        vpn_status = await vpn_manager.status()
        if vpn_status.state in (VpnState.DOWN, VpnState.ERROR):
            vpn_warning = (
                "\n\n\u26a0\ufe0f VPN is currently down. Issue creation may fail "
                "until connectivity is restored."
            )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "\u2705 Create Issue", callback_data="jira_confirm"
                    ),
                    InlineKeyboardButton(
                        "\u274c Cancel", callback_data="jira_cancel"
                    ),
                ]
            ]
        )
        await incoming.reply_text(
            f"{preview}{vpn_warning}",
            reply_markup=keyboard,
        )

    async def handle_issue_callback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the Create Issue / Cancel inline-button press."""

        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return

        await query.answer()

        if query.data == "jira_cancel":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            if query.message is not None:
                await query.message.reply_text("Issue creation cancelled.")
            return

        if query.data != "jira_confirm":
            return

        # Remove buttons immediately to prevent double-clicks.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        template = (
            context.user_data.pop("pending_template", None)
            if context.user_data is not None
            else None
        )
        if template is None:
            if query.message is not None:
                await query.message.reply_text(
                    "No pending issue found. Please forward a message again."
                )
            return

        credentials = await user_store.get(user.id)
        if credentials is None:
            if query.message is not None:
                await query.message.reply_text(
                    "Your Jira session is not configured. "
                    "Use /auth to connect first."
                )
            return

        if query.message is not None:
            await query.message.reply_text(
                "\U0001f504 Creating Jira issue..."
            )

        from .jira_client import JiraClientError

        try:
            result = await jira_client.create_issue(
                credentials.jira_pat, template
            )
        except JiraClientError as error:
            LOGGER.error("Jira issue creation failed (%s)", type(error).__name__)
            if query.message is not None:
                await query.message.reply_text(
                    f"\u274c Failed to create issue: {error}"
                )
            return

        if query.message is not None:
            await query.message.reply_text(
                f"\u2705 Issue created: {result.key}\n{result.url}"
            )

    return (
        MessageHandler(ForwardOrReplyToForwardFilter(), analyze_forward),
        CallbackQueryHandler(
            handle_issue_callback, pattern=r"^jira_(confirm|cancel)$"
        ),
    )
