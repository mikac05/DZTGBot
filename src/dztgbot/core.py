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
    """Build the forward analysis handler and issue-confirmation callback with multi-message batching."""

    async def analyze_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Extract forwarded messages, buffer them safely with a sliding window, and analyze."""

        incoming = update.effective_message
        user = update.effective_user
        if incoming is None or user is None or context.user_data is None:
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

        lock: asyncio.Lock = context.user_data.setdefault("batch_lock", asyncio.Lock())
        async with lock:
            batch: list[ForwardedMessage] = context.user_data.setdefault("pending_batch", [])
            batch.append(record)
            batch_count = len(batch)
            context.user_data["last_forward_time"] = asyncio.get_running_loop().time()

            status_msg = context.user_data.get("batch_status_msg")
            if status_msg is None:
                status_msg = await incoming.reply_text(
                    f"\U0001f4e5 已接收 {batch_count} 条转发消息，等待合并...\n"
                    "(2.5 秒内继续转发将合并为同一工单)"
                )
                context.user_data["batch_status_msg"] = status_msg
            else:
                try:
                    await status_msg.edit_text(
                        f"\U0001f4e5 已接收 {batch_count} 条转发消息，等待合并...\n"
                        "(2.5 秒内继续转发将合并为同一工单)"
                    )
                except Exception:
                    pass

            worker_active = context.user_data.get("batch_worker_active", False)
            if not worker_active:
                context.user_data["batch_worker_active"] = True

                async def batch_worker() -> None:
                    loop = asyncio.get_running_loop()
                    while True:
                        await asyncio.sleep(0.5)
                        async with lock:
                            now = loop.time()
                            last_time = context.user_data.get("last_forward_time", 0.0)
                            if now - last_time >= 2.5:
                                current_batch = list(context.user_data.pop("pending_batch", []))
                                context.user_data["batch_worker_active"] = False
                                current_status = context.user_data.pop("batch_status_msg", None)
                                break

                    if not current_batch:
                        return

                    if current_status is not None:
                        try:
                            await current_status.edit_text(
                                f"\U0001f916 正在分析 {len(current_batch)} 条转发消息，生成统一的 Jira 工单..."
                            )
                        except Exception:
                            pass

                    try:
                        template = await analyzer.analyze(current_batch)
                    except Exception as error:
                        LOGGER.error("Gemini analysis failed (%s: %s)", type(error).__name__, error)
                        await incoming.reply_text(
                            "\u274c Gemini 分析失败或未返回有效结果，请稍后再试。"
                        )
                        return

                    from .analysis import jira_template_preview

                    preview = jira_template_preview(template)
                    context.user_data["pending_template"] = template

                    credentials = await user_store.get(user.id)
                    if credentials is None:
                        await incoming.reply_text(
                            f"{preview}\n\n"
                            "\u26a0\ufe0f 您尚未绑定 Jira 账号，请先在私聊中使用 /auth 进行绑定，然后再进行转发。"
                        )
                        return

                    vpn_warning = ""
                    vpn_status = await vpn_manager.status()
                    if vpn_status.state in (VpnState.DOWN, VpnState.ERROR):
                        vpn_warning = (
                            "\n\n\u26a0\ufe0f VPN 当前处于断开状态，创建工单可能会失败。"
                        )

                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "\u2705 创建 Jira 工单", callback_data="jira_confirm"
                                ),
                                InlineKeyboardButton(
                                    "\u274c 取消", callback_data="jira_cancel"
                                ),
                            ]
                        ]
                    )
                    await incoming.reply_text(
                        f"{preview}{vpn_warning}",
                        reply_markup=keyboard,
                    )

                asyncio.create_task(batch_worker())

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
                await query.message.reply_text("已取消工单创建。")
            return

        if query.data != "jira_confirm":
            return

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
                    "未找到待创建的工单草稿，请重新转发消息。"
                )
            return

        credentials = await user_store.get(user.id)
        if credentials is None:
            if query.message is not None:
                await query.message.reply_text(
                    "未检测到您的 Jira 账号绑定，请先使用 /auth 进行绑定。"
                )
            return

        if query.message is not None:
            await query.message.reply_text(
                "\U0001f504 正在提交创建 Jira 工单..."
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
                    f"\u274c 工单创建失败: {error}"
                )
            return

        if query.message is not None:
            await query.message.reply_text(
                f"\u2705 Jira 工单创建成功: {result.key}\n{result.url}"
            )

    return (
        MessageHandler(ForwardOrReplyToForwardFilter(), analyze_forward),
        CallbackQueryHandler(
            handle_issue_callback, pattern=r"^jira_(confirm|cancel)$"
        ),
    )
