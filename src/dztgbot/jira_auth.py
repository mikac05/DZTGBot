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
            [["📝 手动创建 Jira 工单"]],
            resize_keyboard=True,
        )

        credentials = await user_store.get(user.id)
        if credentials:
            await message.reply_text(
                f"👋 欢迎使用 DZTGBot！当前绑定的 Jira 账号: "
                f'"{credentials.jira_display_name}" '
                f"({credentials.jira_username}).\n\n"
                "您可以直接转发消息给机器人生成工单，或点击下方 [📝 手动创建 Jira 工单] / 使用 /new 手动创建。\n\n"
                "常用命令：\n"
                "/new — 📝 手动创建 Jira 工单\n"
                "/auth — 🔑 重新绑定 Jira 账号\n"
                "/logout — 🚪 解绑 Jira 账号",
                reply_markup=keyboard,
            )
        else:
            await message.reply_text(
                "👋 欢迎使用 DZTGBot！\n\n"
                "转发任何消息给机器人，或点击下方 [📝 手动创建 Jira 工单] 按钮即可快速创建 Jira 工单。\n\n"
                f"使用前请先发送 /auth 绑定您的 Jira 账号。",
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
                "🔒 为了您的账号安全，请在与机器人的私聊窗口中发送 /auth 进行绑定。\n"
                "点击机器人头像即可开启私聊。"
            )
            return ConversationHandler.END

        await message.reply_text(
            "🔑 <b>绑定您的 Jira 账号</b>\n\n"
            "请直接发送您的 Jira 认证凭据，支持以下格式：\n\n"
            "1. <b>个人访问令牌 (PAT)</b>: 直接发送令牌或 <code>Bearer 令牌内容</code>\n"
            "2. <b>账号密码</b>: <code>用户名:密码</code>\n"
            "3. <b>Session Cookie</b>: <code>JSESSIONID=Cookie内容</code>\n\n"
            "⚠️ <b>安全提示</b>：机器人接收到凭据后将<b>立即自动删除</b>您包含凭据的消息。\n"
            "如需取消绑定，请发送 /cancel。",
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
                "❌ 凭据不能为空。请重新发送您的认证凭据，或发送 /cancel 取消。"
            )
            return AWAITING_PAT

        status_message = await chat.send_message(
            "🔄 正在验证您的 Jira 认证凭据..."
        )

        try:
            jira_user = await jira_client.validate_credentials(pat)
        except JiraClientError as error:
            await status_message.edit_text(
                f"❌ 验证失败: {error}\n\n"
                "请检查您的凭据后重新发送，或发送 /cancel 取消。"
            )
            return AWAITING_PAT

        credentials = JiraCredentials(
            jira_username=jira_user.username,
            jira_display_name=jira_user.display_name,
            jira_pat=pat,
        )
        await user_store.store(user.id, credentials)

        await status_message.edit_text(
            f"✅ <b>Jira 账号绑定成功！</b>\n\n"
            f"已成功验证身份：<b>{jira_user.display_name}</b> ({jira_user.username})\n\n"
            "您可以直接转发消息给机器人生成工单，或点击下方 [📝 手动创建 Jira 工单] 按钮。\n\n"
            "如需解绑请随时发送 /logout。",
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
            await message.reply_text("已取消 Jira 账号绑定操作。")
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
                "🚪 <b>已成功解绑！</b>\n\n您的 Jira 认证信息已安全清除。如需重新绑定请发送 /auth。",
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "未检测到您已绑定的 Jira 账号。"
            )

    auth_conversation = ConversationHandler(
        entry_points=[CommandHandler("auth", auth_entry)],
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
    )
