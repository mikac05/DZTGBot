"""Long-running async entry point for DZTGBot."""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram.ext import Application, ContextTypes

from .admin import build_admin_handlers
from .analysis import GeminiAnalyzer
from .config import Settings
from .core import build_forward_handlers
from .jira_auth import build_auth_handlers
from .jira_client import JiraClient
from .rules import RulesStore
from .user_store import UserStore
from .vpn import NetworkManagerL2tpManager

LOGGER = logging.getLogger(__name__)


async def handle_application_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Log an unexpected handler failure without serializing the Telegram update."""

    error = context.error
    LOGGER.error("Unhandled Telegram handler error (%s)", type(error).__name__)


async def run() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rules_store = RulesStore(settings.jira_rules_path)
    await rules_store.initialize()
    user_store = UserStore(settings.user_credentials_path)
    await user_store.initialize()
    vpn_manager = NetworkManagerL2tpManager(
        enabled=settings.vpn_enabled,
        connection_name=settings.vpn_connection_name,
        profile_path=settings.vpn_profile_path,
        allow_start=settings.vpn_allow_start,
        nmcli_bin=settings.vpn_nmcli_bin,
        sudo_bin=settings.vpn_sudo_bin,
        command_timeout_seconds=settings.vpn_command_timeout_seconds,
    )
    initial_vpn_status = await vpn_manager.status()
    LOGGER.info("Initial VPN state: %s", initial_vpn_status.state)
    jira_client = JiraClient(
        base_url=settings.jira_url,
        verify_ssl=settings.jira_verify_ssl,
        vpn_manager=vpn_manager,
    )
    analyzer = GeminiAnalyzer(
        api_key=settings.gemini_api_key,
        timeout_seconds=settings.gemini_timeout_seconds,
        rules_store=rules_store,
        default_project_key=settings.jira_default_project_key,
    )
    async def post_init(app: Application) -> None:
        from telegram import BotCommand
        try:
            await app.bot.set_my_commands(
                [
                    BotCommand("start", "開始使用 / 查看說明"),
                    BotCommand("new", "📝 手動建立 Jira 工單"),
                    BotCommand("auth", "🔑 綁定 Jira 帳號"),
                    BotCommand("logout", "🚪 解綁 Jira 帳號"),
                    BotCommand("help", "📖 查看使用說明"),
                ]
            )
        except Exception as err:
            LOGGER.warning("Could not set bot commands: %s", err)

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(settings.telegram_concurrent_updates)
        .post_init(post_init)
        .build()
    )
    application.add_error_handler(handle_application_error)

    auth_conversation, start_handler, logout_handler, help_handler = build_auth_handlers(
        user_store, jira_client, settings.jira_url
    )
    from telegram.ext import MessageHandler, filters
    logout_button_handler = MessageHandler(filters.Regex(r"^(🚪 解綁 Jira 帳號|🚪 解绑 Jira 账号)$"), logout_handler.callback)
    help_button_handler = MessageHandler(filters.Regex(r"^(📖 說明|📖 说明)$"), help_handler.callback)

    application.add_handlers(
        [
            auth_conversation,
            start_handler,
            logout_handler,
            logout_button_handler,
            help_handler,
            help_button_handler,
            *build_admin_handlers(
                rules_store,
                settings.telegram_admin_user_ids,
                vpn_manager,
            ),
            *build_forward_handlers(
                analyzer, vpn_manager, user_store, jira_client
            ),
        ]
    )

    updater = application.updater
    if updater is None:
        raise RuntimeError("The Telegram updater is unavailable; polling cannot start.")

    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, stop_requested.set)
        except NotImplementedError:
            break

    polling_started = False
    application_started = False
    try:
        async with application:
            try:
                await updater.start_polling(
                    allowed_updates=("message", "callback_query"),
                    bootstrap_retries=3,
                )
                polling_started = True
                await application.start()
                application_started = True
                LOGGER.info("DZTGBot is running")
                try:
                    await stop_requested.wait()
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
            finally:
                if polling_started:
                    await updater.stop()
                if application_started:
                    await application.stop()
    finally:
        await analyzer.aclose()


def main() -> None:
    # TODO: Add external health checks and service supervision in a later phase.
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOGGER.info("DZTGBot stopped by user.")
    except Exception as error:
        # Avoid default traceback rendering for provider exceptions because request
        # URLs can contain authentication material in some client implementations.
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        LOGGER.critical("DZTGBot terminated (%s)", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
