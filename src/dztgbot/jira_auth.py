"""Telegram conversation handlers for Jira credential management."""

from __future__ import annotations

import logging

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .jira_client import JiraClient, JiraClientError
from .user_store import JiraCredentials, UserStore

LOGGER = logging.getLogger(__name__)

AWAITING_PAT = 0


async def get_main_menu_keyboard(
    user_id: int | None, user_store: UserStore
) -> ReplyKeyboardMarkup:
    """Build a dynamic 2-row main menu keyboard based on user's auth status."""
    is_authed = False
    if user_id is not None:
        credentials = await user_store.get(user_id)
        is_authed = credentials is not None

    auth_button = (
        KeyboardButton("🚪 解綁 Jira 帳號")
        if is_authed
        else KeyboardButton("🔑 綁定 Jira 帳號")
    )

    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📝 手動建立 Jira 工單")],
            [auth_button, KeyboardButton("📖 說明")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_auth_handlers(
    user_store: UserStore,
    jira_client: JiraClient,
    jira_url: str,
) -> tuple[ConversationHandler, BaseHandler, BaseHandler, BaseHandler]:
    """Build /start, /auth conversation, /logout, and /help handlers."""

    async def start_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Greet the user and show connection status."""

        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        keyboard = await get_main_menu_keyboard(user.id, user_store)
        credentials = await user_store.get(user.id)
        if credentials:
            await message.reply_text(
                f"👋 歡迎使用 DZTGBot！\n"
                f"目前綁定帳號：<b>{html_escape(credentials.jira_display_name)}</b> ({html_escape(credentials.jira_username)})\n\n"
                "可以直接轉發 Telegram 訊息生成工單，或點擊下方按鈕開始使用。",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "👋 歡迎使用 DZTGBot！\n\n"
                "請先點擊下方 <b>[🔑 綁定 Jira 帳號]</b> 完成綁定，即可轉發訊息或點擊 <b>[📝 手動建立 Jira 工單]</b> 快速發單。",
                reply_markup=keyboard,
                parse_mode="HTML",
            )

    async def help_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Display short, clear usage instructions in Taiwan Traditional Chinese."""

        message = update.effective_message
        user = update.effective_user
        if message is None:
            return

        keyboard = await get_main_menu_keyboard(user.id if user else None, user_store)
        await message.reply_text(
            "📖 <b>DZTGBot 使用說明</b>\n\n"
            "1️⃣ <b>訊息轉發建單（推薦）</b>：\n"
            "直接將 Telegram 訊息轉發給機器人，AI 自動分析並生成 Jira 工單草稿。\n\n"
            "2️⃣ <b>手動快速建單</b>：\n"
            "• 點擊 <code>[📝 手動建立 Jira 工單]</code>\n"
            "• 或輸入 <code>/new 工單標題</code> 快速建立\n"
            "• 或直接發送訊息（第一行為標題，後續為描述，可附圖片）\n\n"
            "3️⃣ <b>常用指令</b>：\n"
            "/new — 📝 手動建立工單\n"
            "/auth — 🔑 綁定 Jira 帳號 (PAT)\n"
            "/logout — 🚪 解綁 Jira 帳號\n"
            "/help — 📖 查看使用說明",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def auth_entry(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Begin the PAT collection conversation."""

        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return ConversationHandler.END

        if chat.type != "private":
            await message.reply_text(
                "🔒 為了您的帳號安全，請在與機器人的私聊視窗中發送 /auth 進行綁定。\n"
                "點擊機器人頭像即可開啟私聊。"
            )
            return ConversationHandler.END

        await message.reply_text(
            "🔑 <b>綁定您的 Jira 帳號</b>\n\n"
            "請直接發送您的 Jira 認證憑據，支援以下格式：\n\n"
            "1. <b>個人存取令牌 (PAT)</b>: 直接發送令牌或 <code>Bearer 令牌內容</code>\n"
            "2. <b>帳號密碼</b>: <code>使用者名稱:密碼</code>\n"
            "3. <b>Session Cookie</b>: <code>JSESSIONID=Cookie內容</code>\n\n"
            "⚠️ <b>安全提示</b>：機器人收到憑據後將<b>立即自動刪除</b>您的訊息。\n"
            "如需取消，請發送 /cancel。",
            parse_mode="HTML",
        )
        return AWAITING_PAT

    async def receive_pat(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Validate the submitted PAT and store on success."""

        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return ConversationHandler.END

        pat = (message.text or "").strip()

        # Delete the message containing the token for security
        try:
            await message.delete()
        except Exception:
            pass  # Bot may lack delete permission

        if not pat:
            await chat.send_message(
                "❌ 憑據不能為空，請重新發送您的認證憑據，或發送 /cancel 取消。"
            )
            return AWAITING_PAT

        status_message = await chat.send_message(
            "🔄 正在驗證您的 Jira 認證憑據..."
        )

        try:
            jira_user = await jira_client.validate_credentials(pat)
        except JiraClientError as error:
            await status_message.edit_text(
                f"❌ 驗證失敗: {error}\n\n"
                "請檢查您的憑據後重新發送，或發送 /cancel 取消。"
            )
            return AWAITING_PAT

        credentials = JiraCredentials(
            jira_username=jira_user.username,
            jira_display_name=jira_user.display_name,
            jira_pat=pat,
        )
        await user_store.store(user.id, credentials)

        keyboard = await get_main_menu_keyboard(user.id, user_store)
        await status_message.edit_text(
            f"✅ <b>Jira 帳號綁定成功！</b>\n\n"
            f"已驗證身份：<b>{html_escape(jira_user.display_name)}</b> ({html_escape(jira_user.username)})\n\n"
            "您現在可以直接轉發訊息或點擊下方按鈕建立工單。",
            parse_mode="HTML",
        )
        # Send main menu with updated keyboard (shows 🚪 解綁 Jira 帳號)
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

        message = update.effective_message
        user = update.effective_user
        if message is not None:
            keyboard = await get_main_menu_keyboard(user.id if user else None, user_store)
            await message.reply_text("已取消 Jira 帳號綁定操作。", reply_markup=keyboard)
        return ConversationHandler.END

    async def logout_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Remove stored Jira credentials for the calling user."""

        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        removed = await user_store.remove(user.id)
        keyboard = await get_main_menu_keyboard(user.id, user_store)
        if removed:
            await message.reply_text(
                "🚪 <b>已成功解綁！</b>\n\n您的 Jira 認證資訊已安全清除。如需重新綁定請點擊下方按鈕或發送 /auth。",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "未檢測到您已綁定的 Jira 帳號。",
                reply_markup=keyboard,
            )

    auth_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("auth", auth_entry),
            MessageHandler(filters.Regex(r"^(🔑 綁定 Jira 帳號|🔑 绑定 Jira 账号)$"), auth_entry),
        ],
        states={
            AWAITING_PAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pat),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    return (
        auth_conversation,
        CommandHandler("start", start_command),
        CommandHandler("logout", logout_command),
        CommandHandler("help", help_command),
    )


def html_escape(text: str) -> str:
    import html
    return html.escape(text)
