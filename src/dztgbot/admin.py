"""Admin-only Telegram commands for runtime rules management."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from telegram import Update
from telegram.ext import BaseHandler, CommandHandler, ContextTypes

from .rules import RulesStore, RulesStoreError
from .vpn import NetworkManagerL2tpManager

LOGGER = logging.getLogger(__name__)


def build_admin_handlers(
    rules_store: RulesStore,
    authorised_user_ids: frozenset[int],
    vpn_manager: NetworkManagerL2tpManager,
) -> Sequence[BaseHandler]:
    """Build administrative commands restricted to configured Telegram user IDs."""

    async def require_admin(update: Update) -> bool:
        user = update.effective_user
        message = update.effective_message
        if user is not None and user.id in authorised_user_ids:
            return True
        if message is not None:
            await message.reply_text("You are not authorised to manage this bot.")
        return False

    async def view_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update):
            return
        message = update.effective_message
        if message is None:
            return

        rules = await rules_store.current_rules()
        chunks = _telegram_chunks(rules)
        await message.reply_text(f"Current runtime Jira rules:\n\n{chunks[0]}")
        for chunk in chunks[1:]:
            await message.reply_text(chunk)

    async def replace_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update):
            return
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        candidate = _rules_from_command(message.text, message.reply_to_message)
        if candidate is None:
            await message.reply_text(
                "Provide non-empty rules after /setrules, or reply with /setrules to a text message."
            )
            return

        try:
            await rules_store.replace(candidate)
        except RulesStoreError as error:
            LOGGER.error(
                "Rules update failed for admin user %s (%s)",
                user.id,
                type(error).__name__,
            )
            await message.reply_text(
                "Rules update failed. The previous rules remain active."
            )
            return

        LOGGER.info("Runtime rules replaced by admin user %s", user.id)
        await message.reply_text("Rules updated and reloaded. The new rules are active now.")

    async def vpn_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update):
            return
        message = update.effective_message
        if message is None:
            return

        status = await vpn_manager.status()
        await message.reply_text(status.message)

    async def vpn_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_admin(update):
            return
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        status = await vpn_manager.start()
        LOGGER.info("VPN start requested by admin user %s; state=%s", user.id, status.state)
        await message.reply_text(status.message)

    return (
        CommandHandler("rules", view_rules),
        CommandHandler("setrules", replace_rules),
        CommandHandler("vpn", vpn_status),
        CommandHandler("vpnstart", vpn_start),
    )


def _rules_from_command(command_text: str | None, replied_to: object | None) -> str | None:
    if command_text:
        parts = command_text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()

    if replied_to is not None:
        replied_text = getattr(replied_to, "text", None) or getattr(replied_to, "caption", None)
        if replied_text and replied_text.strip():
            return replied_text.strip()

    return None


def _telegram_chunks(text: str, limit: int = 3500) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]
