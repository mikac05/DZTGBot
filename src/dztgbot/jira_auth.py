"""Telegram conversation handlers for Jira credential management.

Phase 5 (P5-G): private-chat-only, PAT-only authentication with a configured
conversation timeout (default three minutes). Credential messages are deleted
best-effort; deletion failure warns with a fixed privacy-safe message. Never
logs message content, PAT values, or provider error text.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import DEFAULT_AUTH_TTL_SECONDS
from .domain.policy import (
    DenialCode,
    credential_policy_decision,
    may_disclose_jira_identity,
    normalize_pat_input,
    require_allowed_user,
    require_private_chat,
    user_message_for_denial,
)
from .jira_client import JiraClient, JiraClientError
from .user_store import JiraCredentials, UserStore, UserStoreError

from .ui.i18n import get_text

LOGGER = logging.getLogger(__name__)

AWAITING_PAT = 0

# Conversation user_data key for auth conversation start (UTC, aware).
AUTH_STARTED_AT_KEY = "auth_started_at"

# Reply-keyboard labels in all supported languages that must never be treated as credential material.
_MENU_BUTTON_TEXTS = frozenset(
    {
        # Traditional Chinese
        "🔑 連結 Jira",
        "🔑 綁定 Jira 帳號",
        "🚪 Logout",
        "🚪 解綁 Jira 帳號",
        "📋 指派給我的",
        "🚩 我建的",
        "🔍 搜尋",
        "📝 新建",
        "📝 手動建立 Jira 工單",
        "📊 站會報告",
        "🌐 語言設置",
        "📖 說明",
        # English
        "🔑 Link Jira",
        "📋 Assigned to Me",
        "🚩 Created by Me",
        "🔍 Search",
        "📝 New Issue",
        "📊 Standup Report",
        "🌐 Language",
        "📖 Help",
        # Simplified Chinese
        "🔑 绑定 Jira",
        "🔑 绑定 Jira 账号",
        "🚪 退出登录",
        "🚪 解绑 Jira 账号",
        "📋 指派给我的",
        "🚩 我创建的",
        "🔍 搜索",
        "📝 新建工单",
        "🌐 语言设置",
        "📖 使用说明",
        # Language selection buttons
        "🇹🇼 繁體中文",
        "🇺🇸 English",
        "🇨🇳 简体中文",
    }
)

# Fixed user-visible messages (no credentials, provider bodies, or secret paths).
_AUTH_PROMPT = (
    "🔑 <b>綁定您的 Jira 帳號</b>\n\n"
    "請發送您的 <b>Jira 個人存取令牌 (PAT)</b>。\n\n"
    "⚠️ <b>安全提示</b>：機器人收到憑據後將<b>處理並刪除</b>您的訊息。\n"
    "如需取消，請發送 /cancel。"
)

# --- TEMPORARY BASIC AUTH COMPATIBILITY PROMPT (EASY TO REMOVE LATER) ---
_AUTH_PROMPT_BASIC_ALLOWED = (
    "🔑 <b>綁定您的 Jira 帳號</b>\n\n"
    "請發送您的 <b>Jira 個人存取令牌 (PAT)</b>，\n"
    "或發送 <code>帳號:密碼</code> (例如 <code>username:password</code>)。\n\n"
    "⚠️ <b>安全提示</b>：機器人收到憑據後將<b>處理並刪除</b>您的訊息。\n"
    "如需取消，請發送 /cancel。"
)
# --- END TEMPORARY BASIC AUTH COMPATIBILITY PROMPT ---

_GROUP_REDIRECT = (
    "🔒 為了您的帳號安全，請在與機器人的私聊視窗中操作。\n"
    "點擊機器人頭像即可開啟私聊。"
)

_LOGOUT_SUCCESS = (
    "🚪 <b>已成功解除本機綁定</b>\n\n"
    "本機已清除您的 Jira 認證資料。"
    "此操作不會撤銷 Jira 端的個人存取令牌 (PAT)；"
    "如需撤銷請至 Jira 自行處理。\n\n"
    "如需重新綁定請點擊下方按鈕或發送 /auth。"
)

_LOGOUT_NONE = "未檢測到您已綁定的 Jira 帳號。"

_STORE_FAILURE = "綁定失敗：無法安全儲存認證資料。先前狀態已保留，請稍後重試或發送 /cancel。"

_VALIDATION_FAILURE = (
    "❌ 驗證失敗。請確認您的個人存取令牌後重新發送，或發送 /cancel 取消。"
)

_CANCELLED = "已取消 Jira 帳號綁定操作。"

_LATE_INPUT_ENDED = "綁定操作已結束。如需綁定請重新發送 /auth。"


async def get_main_menu_keyboard(
    user_id: int | None, user_store: UserStore
) -> ReplyKeyboardMarkup:
    """Build a dynamic main menu keyboard based on user's auth status and language preference."""
    is_authed = False
    lang = "zh_TW"
    if user_id is not None:
        credentials = await user_store.get(user_id)
        is_authed = credentials is not None
        lang = await user_store.get_language(user_id)

    if is_authed:
        return ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton(get_text("btn_my_issues", lang)),
                    KeyboardButton(get_text("btn_created_issues", lang)),
                ],
                [
                    KeyboardButton(get_text("btn_search", lang)),
                    KeyboardButton(get_text("btn_new", lang)),
                ],
                [
                    KeyboardButton(get_text("btn_standup", lang)),
                    KeyboardButton(get_text("btn_language", lang)),
                    KeyboardButton(get_text("btn_logout", lang)),
                ],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(get_text("btn_auth", lang)),
                KeyboardButton(get_text("btn_language", lang)),
                KeyboardButton(get_text("btn_help", lang)),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_auth_conversation_expired(
    started_at: datetime | None,
    *,
    now: datetime,
    ttl_seconds: int,
) -> bool:
    """Return True when the auth conversation has no valid start or exceeded TTL."""

    if started_at is None:
        return True
    if started_at.tzinfo is None:
        return True
    return now >= started_at + timedelta(seconds=ttl_seconds)


def _clear_auth_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(AUTH_STARTED_AT_KEY, None)


def _mark_auth_started(context: ContextTypes.DEFAULT_TYPE, started_at: datetime) -> None:
    if context.user_data is not None:
        context.user_data[AUTH_STARTED_AT_KEY] = started_at


def _auth_started_at(context: ContextTypes.DEFAULT_TYPE) -> datetime | None:
    if context.user_data is None:
        return None
    value = context.user_data.get(AUTH_STARTED_AT_KEY)
    return value if isinstance(value, datetime) else None


def _is_menu_button_text(text: str) -> bool:
    return text.strip() in _MENU_BUTTON_TEXTS


async def _delete_credential_message(
    message: object,
    chat: object,
    *,
    user_id: int | None,
) -> bool:
    """Best-effort delete of a credential-bearing message.

    Returns True when deletion succeeded. On failure, warns the user with a
    fixed privacy-safe message and never logs message content.
    """

    try:
        await message.delete()  # type: ignore[attr-defined]
        return True
    except Exception:
        LOGGER.warning(
            "Credential message deletion failed for user %s",
            user_id if user_id is not None else "unknown",
        )
        try:
            send = getattr(chat, "send_message", None)
            if send is not None:
                await send(user_message_for_denial(DenialCode.CREDENTIAL_DELETE_FAILED))
            else:
                reply = getattr(message, "reply_text", None)
                if reply is not None:
                    await reply(
                        user_message_for_denial(DenialCode.CREDENTIAL_DELETE_FAILED)
                    )
        except Exception:
            LOGGER.warning(
                "Could not deliver credential-delete warning for user %s",
                user_id if user_id is not None else "unknown",
            )
        return False


def build_auth_handlers(
    user_store: UserStore,
    jira_client: JiraClient,
    jira_url: str,
    *,
    auth_ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
    allowed_user_ids: frozenset[int] | None = None,
    auth_pat_only: bool = True,
) -> tuple[ConversationHandler, BaseHandler, BaseHandler, BaseHandler]:
    """Build /start, /auth conversation, /logout, and /help handlers.

    ``jira_url`` is retained for call-site compatibility; validation uses the
    injected ``jira_client``. ``auth_ttl_seconds`` defaults to the configured
    three-minute first-release default.

    ``allowed_user_ids`` is the optional deployment allowlist. ``None`` or empty
    means unrestricted (backward compatible). When set, private identity-bearing
    and credential-sensitive entry points enforce ``require_allowed_user``.
    """

    del jira_url  # signature stability for composition root; client owns base URL
    ttl_seconds = int(auth_ttl_seconds)
    if ttl_seconds < 1:
        ttl_seconds = DEFAULT_AUTH_TTL_SECONDS

    # Snapshot allowlist for closures; never log membership contents.
    allowlist = allowed_user_ids

    def _actor_is_allowed(actor_user_id: int) -> bool:
        return require_allowed_user(actor_user_id, allowlist).allowed

    async def _deny_not_allowed(message: object) -> None:
        """Fixed privacy-safe allowlist denial (no auth/credential state)."""

        reply = getattr(message, "reply_text", None)
        if reply is not None:
            await reply(user_message_for_denial(DenialCode.NOT_ALLOWED_USER))

    async def start_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Greet the user; identity disclosure is private-chat only."""

        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return

        chat_type = getattr(chat, "type", None)
        if not require_private_chat(chat_type).allowed:
            # Safe group copy: no identity, auth status, rules, or VPN state.
            await message.reply_text(_GROUP_REDIRECT)
            return

        if not _actor_is_allowed(user.id):
            # Do not disclose identity, auth status, or menu binding state.
            await _deny_not_allowed(message)
            return

        keyboard = await get_main_menu_keyboard(user.id, user_store)
        credentials = None
        if may_disclose_jira_identity(chat_type):
            credentials = await user_store.get(user.id)

        if credentials is not None:
            await message.reply_text(
                f"👋 歡迎使用 DZTGBot！\n"
                f"目前綁定帳號：<b>{html_escape(credentials.jira_display_name)}</b>"
                f" ({html_escape(credentials.jira_username)})\n\n"
                "可以直接轉發 Telegram 訊息生成工單，或點擊下方按鈕開始使用。",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "👋 歡迎使用 DZTGBot！\n\n"
                "請先點擊下方 <b>[🔑 綁定 Jira 帳號]</b> 完成綁定，"
                "即可轉發訊息或點擊 <b>[📝 手動建立 Jira 工單]</b> 快速發單。",
                reply_markup=keyboard,
                parse_mode="HTML",
            )

    async def help_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Display short usage instructions (private-oriented, no secrets)."""

        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None:
            return

        chat_type = getattr(chat, "type", None) if chat is not None else None
        if not require_private_chat(chat_type).allowed:
            await message.reply_text(_GROUP_REDIRECT)
            return

        if user is None or not _actor_is_allowed(user.id):
            await _deny_not_allowed(message)
            return

        keyboard = await get_main_menu_keyboard(user.id, user_store)
        await message.reply_text(
            "📖 <b>DZTGBot Jira 助手使用指南</b>\n\n"
            "🤖 <b>一、AI 智慧轉發建單</b>\n"
            "• <b>轉發訊息建單：</b> 將任何對話或圖片轉發給機器人，AI 自動提取標題與內容產出草稿，點擊 <code>[✅ 確定提交]</code> 即可發布。\n"
            "• <b>PROD 單號自動關聯：</b> 轉發內容含 <code>PROD-xxx</code> 時，自動建立非 PROD 開發單，並自動連結 (Relates to) 原 PROD 單號。\n\n"
            "🔍 <b>二、工單搜尋與快速選單</b>\n"
            "• <b>快捷選單按鈕：</b>\n"
            "  - <code>[📋 指派給我的]</code>：列出指派給您的未解決工單 (/my)\n"
            "  - <code>[🚩 我建的]</code>：列出您發起的工單 (/created)\n"
            "  - <code>[🔍 搜尋]</code>：輸入 <code>/s 關鍵字</code> 搜尋工單 (/s)\n"
            "  - <code>[📝 新建]</code>：手動建立工單草稿 (/new)\n"
            "• <b>單號/網址自動開啟：</b> 在對話中發送 <code>PROJ-123</code> 或 Jira 網址，自動浮現互動卡片。\n"
            "• <b>Inline 搜尋：</b> 在任何聊天視窗輸入 <code>@機器人名稱 關鍵字</code> 即可分享工單。\n\n"
            "⚡ <b>三、工單卡片快捷操作 (Issue Card)</b>\n"
            "• <b>工作流轉移：</b> 點擊 <code>[▶️ 開始開發]</code> 或 <code>[➡️ Move]</code> 切換 Jira 狀態。\n"
            "• <b>回覆卡片新增留言/附件：</b> 直接<b>回覆 (Reply)</b> 卡片訊息（發送文字或照片），自動同步至 Jira！\n"
            "• <b>指派與阻礙：</b> 點擊 <code>[👤 指派]</code> 快速指派，或點擊 <code>[⚠️ 阻礙]</code> 標記 Impediment 阻礙。\n"
            "• <b>子任務建立：</b> 點擊 <code>[➕ 子任務]</code> 發起關聯 Sub-task 草稿。\n\n"
            "🔔 <b>四、未讀通知推播</b>\n"
            "• 每 5 分鐘自動輪詢並推播您的待辦異動與最新留言至 Telegram。\n\n"
            "🔑 <b>五、帳號管理</b>\n"
            "• <code>[🔑 連結 Jira]</code> 或 <code>/auth</code>：綁定 Jira 個人存取令牌 (PAT) 或 帳號:密碼。\n"
            "• <code>[🚪 Logout]</code> 或 <code>/logout</code>：解綁本機憑據。",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def auth_entry(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Begin the PAT collection conversation (private chat only)."""

        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or chat is None:
            return ConversationHandler.END

        if not require_private_chat(getattr(chat, "type", None)).allowed:
            await message.reply_text(_GROUP_REDIRECT)
            return ConversationHandler.END

        if user is None or not _actor_is_allowed(user.id):
            await _deny_not_allowed(message)
            return ConversationHandler.END

        _mark_auth_started(context, _utc_now())
        prompt_text = _AUTH_PROMPT if auth_pat_only else _AUTH_PROMPT_BASIC_ALLOWED
        await message.reply_text(prompt_text, parse_mode="HTML")
        return AWAITING_PAT

    async def receive_pat(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Validate PAT-only input; reject password/basic/cookie and late menu text."""

        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            _clear_auth_session(context)
            return ConversationHandler.END

        if not require_private_chat(getattr(chat, "type", None)).allowed:
            _clear_auth_session(context)
            await message.reply_text(_GROUP_REDIRECT)
            return ConversationHandler.END

        raw_text = message.text if isinstance(message.text, str) else ""
        stripped = raw_text.strip()

        if not _actor_is_allowed(user.id):
            # Defense in depth: never validate/store for non-allowlisted actors.
            # Delete potential credential material; never treat menu labels as PAT.
            _clear_auth_session(context)
            if stripped and not _is_menu_button_text(stripped):
                await _delete_credential_message(message, chat, user_id=user.id)
            await _deny_not_allowed(message)
            return ConversationHandler.END

        now = _utc_now()
        started = _auth_started_at(context)
        expired = _is_auth_conversation_expired(
            started, now=now, ttl_seconds=ttl_seconds
        )

        # Menu / ordinary late input must never be treated as credentials.
        if _is_menu_button_text(stripped):
            _clear_auth_session(context)
            keyboard = await get_main_menu_keyboard(user.id, user_store)
            if expired:
                await message.reply_text(
                    user_message_for_denial(DenialCode.AUTH_EXPIRED),
                    reply_markup=keyboard,
                )
            else:
                await message.reply_text(_LATE_INPUT_ENDED, reply_markup=keyboard)
            return ConversationHandler.END

        # Potential credential material: delete best-effort before any processing.
        await _delete_credential_message(message, chat, user_id=user.id)

        if expired:
            _clear_auth_session(context)
            keyboard = await get_main_menu_keyboard(user.id, user_store)
            await chat.send_message(
                user_message_for_denial(DenialCode.AUTH_EXPIRED),
                reply_markup=keyboard,
            )
            return ConversationHandler.END

        decision = credential_policy_decision(stripped, pat_only=auth_pat_only)
        if not decision.allowed:
            code = decision.denial_code or DenialCode.CREDENTIAL_FORMAT_REJECTED
            await chat.send_message(user_message_for_denial(code))
            # Empty / wrong shape: remain in conversation until TTL or cancel.
            return AWAITING_PAT

        pat = normalize_pat_input(stripped, pat_only=auth_pat_only)
        if pat is None:
            await chat.send_message(
                user_message_for_denial(DenialCode.CREDENTIAL_FORMAT_REJECTED)
            )
            return AWAITING_PAT

        status_message = await chat.send_message("🔄 正在驗證您的 Jira 個人存取令牌...")

        try:
            jira_user = await jira_client.validate_credentials(pat)
        except JiraClientError:
            LOGGER.warning(
                "Jira credential validation failed for user %s",
                user.id,
            )
            try:
                await status_message.edit_text(_VALIDATION_FAILURE)
            except Exception:
                await chat.send_message(_VALIDATION_FAILURE)
            return AWAITING_PAT
        except Exception:
            LOGGER.warning(
                "Unexpected credential validation error for user %s (%s)",
                user.id,
                "unexpected",
            )
            try:
                await status_message.edit_text(_VALIDATION_FAILURE)
            except Exception:
                await chat.send_message(_VALIDATION_FAILURE)
            return AWAITING_PAT

        credentials = JiraCredentials(
            jira_username=jira_user.username,
            jira_display_name=jira_user.display_name,
            jira_pat=pat,
        )
        try:
            await user_store.store(user.id, credentials)
        except (UserStoreError, OSError, RuntimeError):
            LOGGER.error(
                "Credential store failed for user %s (failure-preserving)",
                user.id,
            )
            try:
                await status_message.edit_text(_STORE_FAILURE)
            except Exception:
                await chat.send_message(_STORE_FAILURE)
            return AWAITING_PAT

        _clear_auth_session(context)
        keyboard = await get_main_menu_keyboard(user.id, user_store)
        await status_message.edit_text(
            f"✅ <b>Jira 帳號綁定成功！</b>\n\n"
            f"已驗證身份：<b>{html_escape(jira_user.display_name)}</b>"
            f" ({html_escape(jira_user.username)})\n\n"
            "您現在可以直接轉發訊息或點擊下方按鈕建立工單。",
            parse_mode="HTML",
        )
        await chat.send_message("功能表已更新：", reply_markup=keyboard)

        LOGGER.info(
            "Telegram user %s authenticated as Jira user %s",
            user.id,
            jira_user.username,
        )
        return ConversationHandler.END

    async def cancel(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Cancel the auth conversation."""

        _clear_auth_session(context)
        message = update.effective_message
        user = update.effective_user
        if message is not None:
            keyboard = await get_main_menu_keyboard(
                user.id if user else None, user_store
            )
            await message.reply_text(_CANCELLED, reply_markup=keyboard)
        return ConversationHandler.END

    async def auth_timeout(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """ConversationHandler timeout: end without consuming late input as PAT."""

        _clear_auth_session(context)
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        keyboard = await get_main_menu_keyboard(
            user.id if user else None, user_store
        )
        text = user_message_for_denial(DenialCode.AUTH_EXPIRED)
        try:
            if message is not None:
                await message.reply_text(text, reply_markup=keyboard)
            elif chat is not None:
                await chat.send_message(text, reply_markup=keyboard)
        except Exception:
            LOGGER.warning("Could not deliver auth timeout notice")
        return ConversationHandler.END

    async def logout_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Remove local Jira credentials. Does not revoke the PAT at Jira."""

        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None:
            return

        chat_type = getattr(chat, "type", None) if chat is not None else None
        if not require_private_chat(chat_type).allowed:
            # Do not reveal whether credentials exist.
            await message.reply_text(_GROUP_REDIRECT)
            return

        if not _actor_is_allowed(user.id):
            # Do not reveal whether credentials exist or mutate the store.
            await _deny_not_allowed(message)
            return

        _clear_auth_session(context)
        try:
            removed = await user_store.remove(user.id)
        except (UserStoreError, OSError, RuntimeError):
            LOGGER.error("Credential remove failed for user %s", user.id)
            keyboard = await get_main_menu_keyboard(user.id, user_store)
            await message.reply_text(
                "解綁失敗：無法更新本機認證儲存。請稍後重試。",
                reply_markup=keyboard,
            )
            return

        keyboard = await get_main_menu_keyboard(user.id, user_store)
        if removed:
            await message.reply_text(
                _LOGOUT_SUCCESS,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                _LOGOUT_NONE,
                reply_markup=keyboard,
            )

    async def language_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Display language selection inline keyboard."""

        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        lang = await user_store.get_language(user.id)
        prompt = get_text("lang_select_prompt", lang)
        await message.reply_text(
            prompt,
            reply_markup=build_language_selection_keyboard(),
            parse_mode="HTML",
        )

    async def handle_language_callback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process language selection callback."""

        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        await query.answer()
        data = query.data or ""
        if data.startswith("set_lang:"):
            selected_lang = data.split(":")[1]
            await user_store.set_language(user.id, selected_lang)
            keyboard = await get_main_menu_keyboard(user.id, user_store)
            notice = get_text("lang_changed_notice", selected_lang)
            try:
                await query.edit_message_text(notice, parse_mode="HTML")
            except Exception:
                pass
            if query.message:
                await query.message.reply_text("✨", reply_markup=keyboard)

    auth_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("auth", auth_entry),
            MessageHandler(
                filters.Regex(r"^(🔑 連結 Jira|🔑 Link Jira|🔑 綁定 Jira 帳號|🔑 绑定 Jira 账号|🔑 绑定 Jira)$"),
                auth_entry,
            ),
        ],
        states={
            AWAITING_PAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pat),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, auth_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=float(ttl_seconds),
        per_user=True,
        per_chat=True,
    )

    return (
        auth_conversation,
        CommandHandler("start", start_command),
        CommandHandler("logout", logout_command),
        CommandHandler("help", help_command),
    )


def build_language_handlers(
    user_store: UserStore,
) -> tuple[CommandHandler, CallbackQueryHandler, MessageHandler]:
    """Build /language command handler, callback handler, and reply button handler."""

    async def language_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        lang = await user_store.get_language(user.id)
        prompt = get_text("lang_select_prompt", lang)
        await message.reply_text(
            prompt,
            reply_markup=build_language_selection_keyboard(),
            parse_mode="HTML",
        )

    async def handle_language_callback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        await query.answer()
        data = query.data or ""
        if data.startswith("set_lang:"):
            selected_lang = data.split(":")[1]
            await user_store.set_language(user.id, selected_lang)
            keyboard = await get_main_menu_keyboard(user.id, user_store)
            notice = get_text("lang_changed_notice", selected_lang)
            try:
                await query.edit_message_text(notice, parse_mode="HTML")
            except Exception:
                pass
            if query.message:
                await query.message.reply_text("✨", reply_markup=keyboard)

    cmd_h = CommandHandler("language", language_command)
    cb_h = CallbackQueryHandler(handle_language_callback, pattern=r"^set_lang:")
    btn_h = MessageHandler(filters.Regex(r"^(🌐 語言設置|🌐 Language|🌐 语言设置)$"), language_command)
    return cmd_h, cb_h, btn_h


def build_language_selection_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for UI language selection."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇹🇼 繁體中文", callback_data="set_lang:zh_TW"),
                InlineKeyboardButton("🇺🇸 English", callback_data="set_lang:en"),
                InlineKeyboardButton("🇨🇳 简体中文", callback_data="set_lang:zh_CN"),
            ]
        ]
    )


def html_escape(text: str) -> str:
    return html.escape(text)
