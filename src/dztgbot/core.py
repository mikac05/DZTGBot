"""Async Telegram forward intake."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

# Maximum number of forwarded messages that can be batched into a single Jira issue
MAX_BATCH_SIZE = 20
# Editing draft timeout in seconds (15 minutes)
EDITING_TIMEOUT_SECONDS = 900

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    ReplyKeyboardMarkup,
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

from .vpn import VpnState

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


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Return the persistent 2-row main menu bottom keyboard."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📝 手動建立 Jira 工單")],
            [KeyboardButton("🔑 綁定 Jira 帳號"), KeyboardButton("🚪 解綁 Jira 帳號")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_draft_keyboard() -> ReplyKeyboardMarkup:
    """Return the interactive draft menu keyboard for quick field toggles."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🏷️ 類型: Task"),
                KeyboardButton("🏷️ 類型: 缺陷"),
                KeyboardButton("🏷️ 類型: 優化"),
                KeyboardButton("🏷️ 類型: Epic"),
            ],
            [
                KeyboardButton("⚡ 優先級: High"),
                KeyboardButton("⚡ 優先級: Medium"),
                KeyboardButton("⚡ 優先級: Low"),
            ],
            [
                KeyboardButton("✅ 確定提交工單"),
                KeyboardButton("❌ 取消草稿"),
            ],
        ],
        resize_keyboard=True,
    )


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

        if forwarded.photo:
            photo_id = forwarded.photo[-1].file_id
            context.user_data.setdefault("pending_photo_file_ids", []).append(photo_id)

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
            if len(batch) >= MAX_BATCH_SIZE:
                await incoming.reply_text(
                    f"⚠️ 單次轉發上限為 {MAX_BATCH_SIZE} 則訊息，請分批處理。"
                )
                return
            batch.append(record)
            batch_count = len(batch)
            context.user_data["last_forward_time"] = asyncio.get_running_loop().time()

            status_msg = context.user_data.get("batch_status_msg")
            if status_msg is None:
                status_msg = await incoming.reply_text(
                    f"\U0001f4e5 已接收 {batch_count} 則轉發訊息，等待合併...\n"
                    "(2.5 秒內繼續轉發將合併為同一工單)"
                )
                context.user_data["batch_status_msg"] = status_msg
            else:
                try:
                    await status_msg.edit_text(
                        f"\U0001f4e5 已接收 {batch_count} 則轉發訊息，等待合併...\n"
                        "(2.5 秒內繼續轉發將合併為同一工單)"
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
                                f"\U0001f916 正在分析 {len(current_batch)} 則轉發訊息，生成統一的 Jira 工單..."
                            )
                        except Exception:
                            pass

                    try:
                        template = await analyzer.analyze(current_batch)
                    except Exception as error:
                        LOGGER.error("Gemini analysis failed (%s: %s)", type(error).__name__, error)
                        await incoming.reply_text(
                            "\u274c Gemini 分析失敗或未傳回有效結果，請稍後再試。"
                        )
                        return

                    from .analysis import jira_template_preview

                    photo_count = len(context.user_data.get("pending_photo_file_ids", []))
                    preview = jira_template_preview(template, photo_count)
                    context.user_data["pending_template"] = template

                    credentials = await user_store.get(user.id)
                    if credentials is None:
                        no_auth_keyboard = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "\u270f\ufe0f 編輯草稿", callback_data="jira_edit"
                                    ),
                                    InlineKeyboardButton(
                                        "\u274c 取消", callback_data="jira_cancel"
                                    ),
                                ]
                            ]
                        )
                        await incoming.reply_text(
                            f"{preview}\n\n"
                            "\u26a0\ufe0f 您尚未綁定 Jira 帳號，請先在私聊視窗中使用 /auth 進行綁定後再建立工單。",
                            reply_markup=no_auth_keyboard,
                        )
                        return

                    vpn_warning = ""
                    vpn_status = await vpn_manager.status()
                    if vpn_status.state in (VpnState.DOWN, VpnState.ERROR):
                        vpn_warning = (
                            "\n\n\u26a0\ufe0f VPN 目前處於中斷狀態，建立工單可能會失敗。"
                        )

                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"🏷️ 類型: {template.issuetype}", callback_data="jira_toggle_type"
                                ),
                                InlineKeyboardButton(
                                    f"⚡ 優先級: {template.priority}", callback_data="jira_toggle_priority"
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "\u2705 建立 Jira 工單", callback_data="jira_confirm"
                                ),
                                InlineKeyboardButton(
                                    "\u270f\ufe0f 完整修改", callback_data="jira_edit"
                                ),
                                InlineKeyboardButton(
                                    "\u274c 取消", callback_data="jira_cancel"
                                ),
                            ],
                        ]
                    )
                    await incoming.reply_text(
                        f"{preview}{vpn_warning}",
                        reply_markup=keyboard,
                    )

                asyncio.create_task(batch_worker())

    async def new_issue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new command or '📝 手動建立 Jira 工單' button press."""

        incoming = update.effective_message
        if incoming is None or context.user_data is None:
            return

        from .analysis import JiraTaskTemplate, jira_template_preview

        # Quick command format: /new <title text>
        quick_title = " ".join(context.args).strip() if context.args else ""

        if quick_title:
            template = JiraTaskTemplate(
                summary=quick_title[:255],
                description=quick_title,
                issuetype="Task",
                labels=["telegram-intake"],
                priority="Medium",
                project_key="NGSSA3",
                components=[],
                assignee=None,
                acceptance_criteria=[],
            )
            context.user_data["pending_template"] = template
            photo_count = len(context.user_data.get("pending_photo_file_ids", []))
            preview = jira_template_preview(template, photo_count)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"🏷️ 類型: {template.issuetype}", callback_data="jira_toggle_type"
                        ),
                        InlineKeyboardButton(
                            f"⚡ 優先級: {template.priority}", callback_data="jira_toggle_priority"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "\u2705 建立 Jira 工單", callback_data="jira_confirm"
                        ),
                        InlineKeyboardButton(
                            "\u270f\ufe0f 完整修改", callback_data="jira_edit"
                        ),
                        InlineKeyboardButton(
                            "\u274c 取消", callback_data="jira_cancel"
                        ),
                    ],
                ]
            )
            await incoming.reply_text(
                f"{preview}",
                reply_markup=keyboard,
            )
            return

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
        context.user_data["editing_draft_time"] = time.monotonic()

        await incoming.reply_text(
            "📝 <b>手動建立 Jira 工單</b>\n\n"
            "請直接發送您要建立的工單內容：\n"
            "• <b>第一行</b>：工單標題\n"
            "• <b>後續行數</b>：詳細描述（可附帶圖片）\n\n"
            "💡 <i>提示：亦可在下方按鈕直接切換類型/優先級！</i>",
            reply_markup=get_draft_keyboard(),
            parse_mode="HTML",
        )

    async def handle_edited_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process user's edited text block when editing_draft is active."""

        incoming = update.effective_message
        if incoming is None or context.user_data is None:
            return

        text_content = incoming.text or incoming.caption or ""
        if incoming.photo:
            photo_id = incoming.photo[-1].file_id
            context.user_data.setdefault("pending_photo_file_ids", []).append(photo_id)

        if not text_content and not incoming.photo:
            return

        raw = text_content.strip()

        if raw in ("❌ 取消草稿", "❌ 取消"):
            context.user_data["editing_draft"] = False
            context.user_data.pop("editing_draft_time", None)
            context.user_data.pop("pending_template", None)
            context.user_data.pop("pending_photo_file_ids", None)
            await incoming.reply_text("已取消草稿。", reply_markup=get_main_menu_keyboard())
            return

        if raw == "✅ 確定提交工單":
            template = context.user_data.get("pending_template")
            if not template or not template.summary or not template.summary.strip():
                await incoming.reply_text("⚠️ 工單標題不能為空，請先發送工單內容（第一行為標題）。", reply_markup=get_draft_keyboard())
                return
            context.user_data["editing_draft"] = False
            # Fall through to process submission using existing template
            raw = f"標題: {template.summary}\n描述:\n{template.description}"

        if raw in ("/new", "📝 手動建立 Jira 工單", "📝 手动创建 Jira 工单"):
            await new_issue_command(update, context)
            return

        if not context.user_data.get("editing_draft"):
            return

        # Auto-expire editing mode after timeout
        draft_start = context.user_data.get("editing_draft_time", 0)
        if draft_start and (time.monotonic() - draft_start) > EDITING_TIMEOUT_SECONDS:
            context.user_data["editing_draft"] = False
            context.user_data.pop("editing_draft_time", None)
            await incoming.reply_text(
                "⏰ 編輯已逾時（超過 15 分鐘），草稿已保存。\n"
                "如需繼續編輯，請重新點擊 [✏️ 完整修改] 或使用 /new。",
                reply_markup=get_main_menu_keyboard(),
            )
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

        # Bottom Reply Keyboard button interactions
        if raw.startswith("🏷️ 類型:"):
            new_type = raw.split(":", 1)[-1].strip()
            updated_template = JiraTaskTemplate(
                summary=original_template.summary,
                description=original_template.description,
                issuetype=new_type,
                labels=original_template.labels,
                priority=original_template.priority,
                project_key=original_template.project_key,
                components=original_template.components,
                assignee=original_template.assignee,
                acceptance_criteria=original_template.acceptance_criteria,
            )
        elif raw.startswith("⚡ 優先級:"):
            new_prio = raw.split(":", 1)[-1].strip()
            updated_template = JiraTaskTemplate(
                summary=original_template.summary,
                description=original_template.description,
                issuetype=original_template.issuetype,
                labels=original_template.labels,
                priority=new_prio,
                project_key=original_template.project_key,
                components=original_template.components,
                assignee=original_template.assignee,
                acceptance_criteria=original_template.acceptance_criteria,
            )
        elif any(raw.lower().startswith(p) for p in ("標題:", "標題：", "标题:", "标题：", "summary:")):
            updated_template = parse_edited_template(text_content, original_template)
        else:
            lines = raw.splitlines()
            parsed_summary = lines[0].strip() if lines else original_template.summary
            parsed_desc = "\n".join(lines[1:]).strip() if len(lines) > 1 else parsed_summary
            updated_template = JiraTaskTemplate(
                summary=parsed_summary or original_template.summary or "未命名工單",
                description=parsed_desc or original_template.description or parsed_summary or "無詳細描述",
                issuetype=original_template.issuetype,
                labels=original_template.labels,
                priority=original_template.priority,
                project_key=original_template.project_key,
                components=original_template.components,
                assignee=original_template.assignee,
                acceptance_criteria=original_template.acceptance_criteria,
            )

        # Validate template fields
        validation_errors = validate_template_fields(updated_template)
        if validation_errors:
            error_msg = "\n".join(f"❌ {err}" for err in validation_errors)
            await incoming.reply_text(
                f"⚠️ <b>工單內容不符合規範，請修正後重新發送：</b>\n\n{html.escape(error_msg)}",
                parse_mode="HTML",
            )
            context.user_data["pending_template"] = updated_template
            return

        user = update.effective_user
        published_key = context.user_data.pop("editing_published_key", None)

        if published_key and user:
            credentials = await user_store.get(user.id)
            if credentials:
                await incoming.reply_text(f"\U0001f504 正在更新 Jira 工單 {published_key}...")
                from .jira_client import JiraClientError
                try:
                    res = await jira_client.update_issue(
                        credentials.jira_pat, published_key, updated_template
                    )
                    photo_file_ids = context.user_data.pop("pending_photo_file_ids", [])
                    uploaded_photos = 0
                    for idx, file_id in enumerate(photo_file_ids, 1):
                        try:
                            tg_file = await context.bot.get_file(file_id)
                            img_bytes = await tg_file.download_as_bytearray()
                            filename = f"updated_image_{idx}.jpg"
                            await jira_client.add_attachment(
                                credentials.jira_pat, res.key, filename, bytes(img_bytes), mime_type="image/jpeg"
                            )
                            uploaded_photos += 1
                        except Exception as err:
                            LOGGER.error("Failed to upload photo %s to Jira issue %s (%s)", file_id, res.key, err)

                    img_status = f"\n<b>附圖</b>: 已成功上傳 {uploaded_photos} 張圖片至 Jira 工單" if uploaded_photos > 0 else ""

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
                                    "\U0001f517 僅複製連結", callback_data="jira_copylink"
                                ),
                                InlineKeyboardButton(
                                    "\U0001f4cb 複製連結與摘要", callback_data="jira_copysummary"
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "\u270f\ufe0f 編輯此工單", callback_data="jira_editpublished"
                                ),
                            ],
                        ]
                    )
                    await incoming.reply_text(
                        f"\u2705 <b>Jira 工單 {html.escape(res.key)} 更新成功！</b>\n{html.escape(res.url)}{img_status}",
                        reply_markup=keyboard,
                        parse_mode="HTML",
                    )
                    return
                except JiraClientError as error:
                    LOGGER.error("Jira issue update failed (%s)", type(error).__name__)
                    await incoming.reply_text(f"\u274c 工單更新失敗: {html.escape(str(error))}")
                    return

        context.user_data["pending_template"] = updated_template
        context.user_data["editing_draft"] = False

        photo_count = len(context.user_data.get("pending_photo_file_ids", []))
        preview = jira_template_preview(updated_template, photo_count)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🏷️ 類型: {updated_template.issuetype}", callback_data="jira_toggle_type"
                    ),
                    InlineKeyboardButton(
                        f"⚡ 優先級: {updated_template.priority}", callback_data="jira_toggle_priority"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "\u2705 建立 Jira 工單", callback_data="jira_confirm"
                    ),
                    InlineKeyboardButton(
                        "\u270f\ufe0f 完整修改", callback_data="jira_edit"
                    ),
                    InlineKeyboardButton(
                        "\u274c 取消", callback_data="jira_cancel"
                    ),
                ],
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
                context.user_data.pop("pending_photo_file_ids", None)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            if query.message is not None:
                await query.message.reply_text("已取消操作。", reply_markup=get_main_menu_keyboard())
            return

        if query.data == "jira_copylink":
            last_pub = context.user_data.get("last_published") if context.user_data else None
            if last_pub and query.message is not None:
                url = last_pub["url"]
                await query.message.reply_text(
                    f"\U0001f517 <b>Jira 工單連結</b>（點擊框內一鍵複製）：\n\n<pre><code>{html.escape(url)}</code></pre>",
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
                    f"\U0001f4cb <b>Jira 工單連結與摘要</b>（點擊框內一鍵複製）：\n\n<pre><code>{html.escape(text_content)}</code></pre>",
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
                    context.user_data["editing_draft_time"] = time.monotonic()

                from .analysis import jira_template_editable_text

                editable_text = jira_template_editable_text(last_pub["template"])
                await query.message.reply_text(
                    f"✏️ <b>編輯已發布工單 ({html.escape(last_pub['key'])})</b>\n\n"
                    "請點擊或複製下方框內文字，在輸入框中修改後發送給機器人直接更新：\n\n"
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
                    await query.message.reply_text("未找到待編輯的工單草稿。")
                return

            context.user_data["editing_draft"] = True
            context.user_data["editing_draft_time"] = time.monotonic()
            from .analysis import jira_template_editable_text

            editable_text = jira_template_editable_text(template)
            if query.message is not None:
                await query.message.reply_text(
                    "✏️ <b>請點擊或複製下方框內文字，修改後再直接發送給機器人：</b>\n\n"
                    f"<pre><code>{html.escape(editable_text)}</code></pre>",
                    parse_mode="HTML",
                )
            return

        if query.data in ("jira_toggle_type", "jira_toggle_priority"):
            if context.user_data is None:
                return
            template = context.user_data.get("pending_template")
            if template is None:
                if query.message is not None:
                    await query.message.reply_text("未找到待編輯的工單草稿。")
                return

            from .analysis import JiraTaskTemplate, jira_template_preview

            if query.data == "jira_toggle_type":
                types = ["Task", "Epic", "缺陷", "優化"]
                cur_idx = types.index(template.issuetype) if template.issuetype in types else 0
                new_type = types[(cur_idx + 1) % len(types)]
                updated_template = JiraTaskTemplate(
                    summary=template.summary,
                    description=template.description,
                    issuetype=new_type,
                    labels=template.labels,
                    priority=template.priority,
                    project_key=template.project_key,
                    components=template.components,
                    assignee=template.assignee,
                    acceptance_criteria=template.acceptance_criteria,
                )
            else:  # jira_toggle_priority
                priorities = ["Medium", "High", "Highest", "Low", "Lowest"]
                cur_idx = priorities.index(template.priority) if template.priority in priorities else 0
                new_priority = priorities[(cur_idx + 1) % len(priorities)]
                updated_template = JiraTaskTemplate(
                    summary=template.summary,
                    description=template.description,
                    issuetype=template.issuetype,
                    labels=template.labels,
                    priority=new_priority,
                    project_key=template.project_key,
                    components=template.components,
                    assignee=template.assignee,
                    acceptance_criteria=template.acceptance_criteria,
                )

            context.user_data["pending_template"] = updated_template
            photo_count = len(context.user_data.get("pending_photo_file_ids", []))
            preview = jira_template_preview(updated_template, photo_count)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"🏷️ 類型: {updated_template.issuetype}", callback_data="jira_toggle_type"
                        ),
                        InlineKeyboardButton(
                            f"⚡ 優先級: {updated_template.priority}", callback_data="jira_toggle_priority"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "\u2705 建立 Jira 工單", callback_data="jira_confirm"
                        ),
                        InlineKeyboardButton(
                            "\u270f\ufe0f 完整修改", callback_data="jira_edit"
                        ),
                        InlineKeyboardButton(
                            "\u274c 取消", callback_data="jira_cancel"
                        ),
                    ],
                ]
            )
            if query.message is not None:
                try:
                    await query.message.edit_text(
                        f"📋 **Jira 工單草稿預覽**（已更新）\n\n{preview}",
                        reply_markup=keyboard,
                    )
                except Exception:
                    pass
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
                    "未找到待建立的工單草稿，請重新轉發訊息。"
                )
            return

        credentials = await user_store.get(user.id)
        if credentials is None:
            if query.message is not None:
                await query.message.reply_text(
                    "未檢測到您的 Jira 帳號綁定，請先使用 /auth 進行綁定。"
                )
            return

        if query.message is not None:
            await query.message.reply_text(
                "\U0001f504 正在提交建立 Jira 工單..."
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
                    f"\u274c 工單建立失敗: {html.escape(str(error))}"
                )
            return

        photo_file_ids = context.user_data.pop("pending_photo_file_ids", []) if context.user_data else []
        uploaded_photos = 0
        if photo_file_ids and query.message is not None:
            await query.message.reply_text(
                f"📷 正在上傳 {len(photo_file_ids)} 張圖片至 Jira 工單..."
            )
        for idx, file_id in enumerate(photo_file_ids, 1):
            try:
                tg_file = await context.bot.get_file(file_id)
                img_bytes = await tg_file.download_as_bytearray()
                filename = f"telegram_image_{idx}.jpg"
                await jira_client.add_attachment(
                    credentials.jira_pat, result.key, filename, bytes(img_bytes), mime_type="image/jpeg"
                )
                uploaded_photos += 1
            except Exception as err:
                LOGGER.error("Failed to upload photo %s to Jira issue %s (%s)", file_id, result.key, err)

        img_status = f"\n<b>附圖</b>: 已成功上傳 {uploaded_photos} 張圖片至 Jira 工單" if uploaded_photos > 0 else ""

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
                        "\U0001f517 僅複製連結", callback_data="jira_copylink"
                    ),
                    InlineKeyboardButton(
                        "\U0001f4cb 複製連結與摘要", callback_data="jira_copysummary"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "\u270f\ufe0f 編輯此工單", callback_data="jira_editpublished"
                    ),
                ],
            ]
        )

        if query.message is not None:
            await query.message.reply_text(
                f"\u2705 <b>Jira 工單建立成功！</b>\n\n"
                f"<b>Key</b>: <code>{html.escape(result.key)}</code>\n"
                f"<b>標題</b>: {html.escape(template.summary)}\n"
                f"<b>連結</b>: {html.escape(result.url)}{img_status}",
                reply_markup=keyboard,
                parse_mode="HTML",
            )

    return (
        CommandHandler("new", new_issue_command),
        MessageHandler(ForwardOrReplyToForwardFilter(), analyze_forward),
        MessageHandler((filters.TEXT | filters.PHOTO) & (~filters.COMMAND), handle_edited_text_input),
        CallbackQueryHandler(
            handle_issue_callback, pattern=r"^jira_(confirm|edit|cancel|copylink|copysummary|editpublished|toggle_type|toggle_priority)$"
        ),
    )
