"""Async Telegram forward intake."""

from __future__ import annotations

import asyncio
import html
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
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from .analysis import JiraTaskTemplate

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
                                    "\u270f\ufe0f 编辑草稿", callback_data="jira_edit"
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

    async def new_issue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new command or '📝 手动创建 Jira 工单' button press."""

        incoming = update.effective_message
        if incoming is None or context.user_data is None:
            return

        from .analysis import JiraTaskTemplate, jira_template_editable_text

        default_template = JiraTaskTemplate(
            summary="",
            description="",
            issuetype="Task",
            labels=["telegram-intake"],
            priority="Medium",
            project_key="NGSSA3",
            components=[],
            assignee=None,
            acceptance_criteria=[],
        )
        context.user_data["pending_template"] = default_template
        context.user_data["editing_draft"] = True

        blank_editable = jira_template_editable_text(default_template)
        await incoming.reply_text(
            "📝 <b>手动创建 Jira 工单</b>\n\n"
            "请点击/复制下方代码框内的完整文字，在输入框中填入各个字段内容后发送给机器人：\n\n"
            f"<pre><code>{html.escape(blank_editable)}</code></pre>",
            parse_mode="HTML",
        )

    async def handle_edited_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process user's edited text block when editing_draft is active."""

        incoming = update.effective_message
        if incoming is None or not incoming.text or context.user_data is None:
            return

        if incoming.text.strip() in ("/new", "📝 手动创建 Jira 工单"):
            await new_issue_command(update, context)
            return

        if not context.user_data.get("editing_draft"):
            return

        if forwarded_message_in(incoming) is not None:
            return

        from .analysis import JiraTaskTemplate, jira_template_preview, parse_edited_template, validate_template_fields

        original_template = context.user_data.get("pending_template")
        if original_template is None:
            original_template = JiraTaskTemplate(
                summary="",
                description="",
                issuetype="Task",
                labels=["telegram-intake"],
                priority="Medium",
                project_key="NGSSA3",
                components=[],
                assignee=None,
                acceptance_criteria=[],
            )

        from .analysis import jira_template_preview, parse_edited_template, validate_template_fields

        updated_template = parse_edited_template(incoming.text, original_template)

        # Validate template fields
        validation_errors = validate_template_fields(updated_template)
        if validation_errors:
            error_msg = "\n".join(f"❌ {err}" for err in validation_errors)
            await incoming.reply_text(
                f"⚠️ <b>工单内容不符合规范，请修正后重新发送：</b>\n\n{html.escape(error_msg)}",
                parse_mode="HTML",
            )
            context.user_data["pending_template"] = updated_template
            return

        user = update.effective_user
        published_key = context.user_data.pop("editing_published_key", None)

        if published_key and user:
            credentials = await user_store.get(user.id)
            if credentials:
                await incoming.reply_text(f"\U0001f504 正在更新 Jira 工单 {published_key}...")
                from .jira_client import JiraClientError
                try:
                    res = await jira_client.update_issue(
                        credentials.jira_pat, published_key, updated_template
                    )
                    context.user_data["editing_draft"] = False
                    context.user_data["last_published"] = {
                        "key": res.key,
                        "url": res.url,
                        "summary": updated_template.summary,
                        "template": updated_template,
                    }
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "\U0001f517 仅复制链接", callback_data="jira_copylink"
                                ),
                                InlineKeyboardButton(
                                    "\U0001f4cb 复制链接与摘要", callback_data="jira_copysummary"
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "\u270f\ufe0f 编辑此工单", callback_data="jira_editpublished"
                                ),
                            ],
                        ]
                    )
                    await incoming.reply_text(
                        f"\u2705 <b>Jira 工单 {html.escape(res.key)} 更新成功！</b>\n{html.escape(res.url)}",
                        reply_markup=keyboard,
                        parse_mode="HTML",
                    )
                    return
                except JiraClientError as error:
                    LOGGER.error("Jira issue update failed (%s)", type(error).__name__)
                    await incoming.reply_text(f"\u274c 工单更新失败: {html.escape(str(error))}")
                    return

        context.user_data["pending_template"] = updated_template
        context.user_data["editing_draft"] = False

        preview = jira_template_preview(updated_template)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "\u2705 创建 Jira 工单", callback_data="jira_confirm"
                    ),
                    InlineKeyboardButton(
                        "\u270f\ufe0f 编辑草稿", callback_data="jira_edit"
                    ),
                    InlineKeyboardButton(
                        "\u274c 取消", callback_data="jira_cancel"
                    ),
                ]
            ]
        )
        await incoming.reply_text(
            f"\u2705 <b>草稿已更新！</b>\n\n{html.escape(preview)}",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def handle_issue_callback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline button actions."""

        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return

        await query.answer()

        if query.data == "jira_cancel":
            if context.user_data is not None:
                context.user_data["editing_draft"] = False
                context.user_data.pop("editing_published_key", None)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            if query.message is not None:
                await query.message.reply_text("已取消操作。")
            return

        if query.data == "jira_copylink":
            last_pub = context.user_data.get("last_published") if context.user_data else None
            if last_pub and query.message is not None:
                url = last_pub["url"]
                await query.message.reply_text(
                    f"\U0001f517 <b>Jira 工单链接</b>（点击框内一键复制）：\n\n<pre><code>{html.escape(url)}</code></pre>",
                    parse_mode="HTML",
                )
            return

        if query.data == "jira_copysummary":
            last_pub = context.user_data.get("last_published") if context.user_data else None
            if last_pub and query.message is not None:
                key = last_pub["key"]
                summary = last_pub["summary"]
                url = last_pub["url"]
                text_content = f"【{key}】{summary}\n{url}"
                await query.message.reply_text(
                    f"\U0001f4cb <b>Jira 工单链接与摘要</b>（点击框内一键复制）：\n\n<pre><code>{html.escape(text_content)}</code></pre>",
                    parse_mode="HTML",
                )
            return

        if query.data == "jira_editpublished":
            last_pub = context.user_data.get("last_published") if context.user_data else None
            if last_pub and query.message is not None:
                if context.user_data is not None:
                    context.user_data["editing_published_key"] = last_pub["key"]
                    context.user_data["pending_template"] = last_pub["template"]
                    context.user_data["editing_draft"] = True

                from .analysis import jira_template_editable_text

                editable_text = jira_template_editable_text(last_pub["template"])
                await query.message.reply_text(
                    f"✏️ <b>编辑已发布工单 ({html.escape(last_pub['key'])})</b>\n\n"
                    "请点击/复制下方代码框内的完整文字，在输入框中修改后发送给机器人直接更新：\n\n"
                    f"<pre><code>{html.escape(editable_text)}</code></pre>",
                    parse_mode="HTML",
                )
            return

        if query.data == "jira_edit":
            if context.user_data is None:
                return
            template = context.user_data.get("pending_template")
            if template is None:
                if query.message is not None:
                    await query.message.reply_text("未找到待编辑的工单草稿。")
                return

            context.user_data["editing_draft"] = True
            from .analysis import jira_template_editable_text

            editable_text = jira_template_editable_text(template)
            if query.message is not None:
                await query.message.reply_text(
                    "✏️ <b>请点击/复制下方代码框内的完整文字，修改后再直接发送给机器人：</b>\n\n"
                    f"<pre><code>{html.escape(editable_text)}</code></pre>",
                    parse_mode="HTML",
                )
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
                    f"\u274c 工单创建失败: {html.escape(str(error))}"
                )
            return

        if context.user_data is not None:
            context.user_data["last_published"] = {
                "key": result.key,
                "url": result.url,
                "summary": template.summary,
                "template": template,
            }

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "\U0001f517 仅复制链接", callback_data="jira_copylink"
                    ),
                    InlineKeyboardButton(
                        "\U0001f4cb 复制链接与摘要", callback_data="jira_copysummary"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "\u270f\ufe0f 编辑此工单", callback_data="jira_editpublished"
                    ),
                ],
            ]
        )

        if query.message is not None:
            await query.message.reply_text(
                f"\u2705 <b>Jira 工单创建成功！</b>\n\n"
                f"<b>Key</b>: <code>{html.escape(result.key)}</code>\n"
                f"<b>标题</b>: {html.escape(template.summary)}\n"
                f"<b>链接</b>: {html.escape(result.url)}",
                reply_markup=keyboard,
                parse_mode="HTML",
            )

    return (
        CommandHandler("new", new_issue_command),
        MessageHandler(ForwardOrReplyToForwardFilter(), analyze_forward),
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_edited_text_input),
        CallbackQueryHandler(
            handle_issue_callback, pattern=r"^jira_(confirm|edit|cancel|copylink|copysummary|editpublished)$"
        ),
    )
