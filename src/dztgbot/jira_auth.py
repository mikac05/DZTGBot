"""Telegram conversation handlers for Jira credential management."""

from __future__ import annotations

import logging

from telegram import ReplyKeyboardMarkup, Update
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


def build_auth_handlers(
    user_store: UserStore,
    jira_client: JiraClient,
    jira_url: str,
) -> tuple[ConversationHandler, BaseHandler, BaseHandler]:
    """Build /start, /auth conversation, and /logout handlers."""

    jira_host = jira_url.split("://", 1)[-1].split("/", 1)[0]

    async def start_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Greet the user and show connection status."""

        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("📝 手動建立 Jira 工單")],
                [KeyboardButton("🔑 綁定 Jira 帳號"), KeyboardButton("🚪 解綁 Jira 帳號")],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

        credentials = await user_store.get(user.id)
        if credentials:
            await message.reply_text(
                f"👋 歡迎使用 DZTGBot！目前綁定的 Jira 帳號: "
                f'"{credentials.jira_display_name}" '
                f"({credentials.jira_username})。\n\n"
                "您可以直接轉發訊息給機器人生成工單，或點擊下方按鈕手動建立。\n\n"
                "常用指令：\n"
                "/new — 📝 手動建立 Jira 工單\n"
                "/auth — 🔑 重新綁定 Jira 帳號\n"
                "/logout — 🚪 解綁 Jira 帳號",
                reply_markup=keyboard,
            )
        else:
            await message.reply_text(
                "👋 歡迎使用 DZTGBot！\n\n"
                "轉發任何訊息給機器人，或點擊下方 [📝 手動建立 Jira 工單] 按鈕即可快速建立 Jira 工單。\n\n"
                "使用前請先點擊下方 [🔑 綁定 Jira 帳號] 或發送 /auth 綁定您的帳號。",
                reply_markup=keyboard,
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

        await status_message.edit_text(
            f"✅ <b>Jira 帳號綁定成功！</b>\n\n"
            f"已成功驗證身份：<b>{jira_user.display_name}</b> ({jira_user.username})\n\n"
            "您可以直接轉發訊息給機器人生成工單，或點擊下方 [📝 手動建立 Jira 工單] 按鈕。\n\n"
            "如需解綁請隨時發送 /logout。",
            parse_mode="HTML",
        )

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
        if message is not None:
            await message.reply_text("已取消 Jira 帳號綁定操作。")
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
        if removed:
            await message.reply_text(
                "🚪 <b>已成功解綁！</b>\n\n您的 Jira 認證資訊已安全清除。如需重新綁定請發送 /auth。",
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "未檢測到您已綁定的 Jira 帳號。"
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
        MessageHandler(filters.Regex(r"^(🚪 解綁 Jira 帳號|🚪 解绑 Jira 账号)$"), logout_command),
    )
