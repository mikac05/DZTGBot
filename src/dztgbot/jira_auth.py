"""Telegram conversation handlers for Jira credential management."""

from __future__ import annotations

import logging

from telegram import Update
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

        credentials = await user_store.get(user.id)
        if credentials:
            await message.reply_text(
                f"👋 Welcome back! You're connected to Jira as "
                f'"{credentials.jira_display_name}" '
                f"({credentials.jira_username}).\n\n"
                "Forward any message to me and I'll help create a Jira issue.\n\n"
                "Commands:\n"
                "/auth — reconnect or change Jira account\n"
                "/logout — disconnect your Jira account"
            )
        else:
            await message.reply_text(
                "👋 Welcome to DZTGBot!\n\n"
                "I help you create Jira issues from Telegram messages. "
                "Forward any message to me, I'll analyze it with AI, "
                "show you a preview, and create the issue on your Jira board "
                "with one tap.\n\n"
                "To get started, use /auth to connect your Jira account."
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
                "🔒 For security, please use /auth in a private chat with me.\n"
                "Tap my name to open a direct conversation."
            )
            return ConversationHandler.END

        await message.reply_text(
            f"🔗 Let's connect your Jira account on {jira_host}.\n\n"
            "Please send your Jira Personal Access Token (PAT).\n\n"
            "To create a PAT: go to Jira → your profile → "
            "Personal Access Tokens → Create token.\n\n"
            "⚠️ I'll delete your message containing the token "
            "immediately for security."
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
                "The token cannot be empty. Please send your PAT, "
                "or /cancel to stop."
            )
            return AWAITING_PAT

        status_message = await chat.send_message(
            "🔄 Validating your token with the Jira server..."
        )

        try:
            jira_user = await jira_client.validate_credentials(pat)
        except JiraClientError as error:
            await status_message.edit_text(
                f"❌ {error}\n\n"
                "Please check your token and try again, or /cancel to stop."
            )
            return AWAITING_PAT

        credentials = JiraCredentials(
            jira_username=jira_user.username,
            jira_display_name=jira_user.display_name,
            jira_pat=pat,
        )
        await user_store.store(user.id, credentials)

        await status_message.edit_text(
            f"✅ Connected! Authenticated as "
            f'"{jira_user.display_name}" ({jira_user.username}) '
            f"on {jira_host}.\n\n"
            "You can now forward messages to me and I'll help create "
            "Jira issues.\n\n"
            "Use /logout to disconnect your Jira account."
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
            await message.reply_text("Authentication cancelled.")
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
                "🔓 Disconnected. Your Jira credentials have been removed.\n\n"
                "Use /auth to reconnect."
            )
        else:
            await message.reply_text(
                "You don't have any stored Jira credentials."
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
