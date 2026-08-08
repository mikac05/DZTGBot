"""Long-running async composition root and lifecycle entry point for DZTGBot."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import logging
from pathlib import Path
import signal
from typing import TYPE_CHECKING, Any

from telegram import BotCommand
from telegram.ext import Application, BaseUpdateProcessor, ContextTypes

from .admin import build_admin_handlers
from .config import Settings
from .domain import Draft
from .domain.callbacks import (
    CallbackAction,
    CallbackParseError,
    hash_opaque_token,
    parse_callback_data,
)
from .infrastructure import (
    AsyncTaskScheduler,
    GeminiGateway,
    JiraGateway,
    JiraTimeouts,
    SQLiteWorkflowRepository,
    SystemClock,
    UuidIdGenerator,
)
from .infrastructure.keyed_processor import KeyedProcessor, WorkKey
from .jira_auth import build_auth_handlers
from .jira_client import JiraClient
from .rules import RulesStore
from .services import (
    AttachmentService,
    CallbackService,
    ConnectivityService,
    IntakeService,
    SubmissionService,
    WorkflowService,
)
from .services.limits import ResourceKind, ResourceLimitSpec, ResourceLimiter
from .services.observability import SafeMetrics
from .ui.handlers import build_production_ui_handlers
from .ui.keyboards import build_draft_inline_keyboard, get_draft_reply_keyboard
from .ui.rendering import render_draft_card
from .user_store import UserStore
from .vpn import NetworkManagerL2tpManager

LOGGER = logging.getLogger(__name__)


class KeyedUpdateProcessor(BaseUpdateProcessor):
    """Adapter integrating KeyedProcessor into python-telegram-bot."""

    def __init__(
        self,
        processor: KeyedProcessor,
        workflow_repo: SQLiteWorkflowRepository,
    ) -> None:
        capacity = processor._capacity
        super().__init__(max_concurrent_updates=capacity)
        self._processor = processor
        self._workflow_repo = workflow_repo

    async def initialize(self) -> None:
        """Initialize the update processor."""
        pass

    async def shutdown(self) -> None:
        """Shutdown the underlying keyed processor."""
        await self._processor.close()

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        key = await self._derive_key(update)
        result = await self._processor.try_run(key, lambda: coroutine)
        if not result.completed and result.feedback:
            await self._send_feedback(update, result.feedback)

    async def _derive_key(self, update: object) -> WorkKey:
        cb_query = getattr(update, "callback_query", None)
        if cb_query is not None:
            raw_data = getattr(cb_query, "data", None)
            if isinstance(raw_data, str) and raw_data.startswith("j1:"):
                try:
                    parsed = parse_callback_data(raw_data)
                    token_hash = hash_opaque_token(parsed.opaque_token)
                    record = await self._workflow_repo.get_callback(token_hash)
                    if record is not None and record.draft_id:
                        return WorkKey.for_workflow(record.draft_id)
                except Exception:
                    pass
        return self._derive_collection_key(update)

    @staticmethod
    def _derive_collection_key(update: object) -> WorkKey:
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        msg = getattr(update, "effective_message", None)

        actor_id = getattr(user, "id", None) if user else None
        chat_id = getattr(chat, "id", None) if chat else None
        thread_id = None
        if msg:
            raw_thread = getattr(msg, "message_thread_id", None)
            if isinstance(raw_thread, int) and raw_thread > 0:
                thread_id = raw_thread

        if isinstance(actor_id, int) and actor_id > 0 and isinstance(chat_id, int) and chat_id != 0:
            try:
                return WorkKey.for_collection(
                    actor_id=actor_id,
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                )
            except ValueError:
                pass
        return WorkKey.for_collection(actor_id=1, chat_id=1, message_thread_id=None)

    @staticmethod
    async def _send_feedback(update: object, feedback: str) -> None:
        cb_query = getattr(update, "callback_query", None)
        if cb_query is not None and hasattr(cb_query, "answer"):
            try:
                res = cb_query.answer(text=feedback, show_alert=True)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await res
                return
            except Exception as err:
                LOGGER.warning("Could not send callback error feedback (%s)", type(err).__name__)

        msg = getattr(update, "effective_message", None)
        if msg is not None and hasattr(msg, "reply_text"):
            try:
                res = msg.reply_text(feedback)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await res
                return
            except Exception as err:
                LOGGER.warning("Could not send message error feedback (%s)", type(err).__name__)


async def handle_application_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Log an unexpected handler failure without serializing the Telegram update."""
    error = context.error
    LOGGER.error("Unhandled Telegram handler error (%s)", type(error).__name__)


async def run() -> None:
    """Run DZTGBot with explicit composition root and deterministic resource lifecycle."""
    settings = Settings.from_environment()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not settings.workflow_db_path:
        raise RuntimeError("WORKFLOW_DB_PATH must be configured in environment/settings")

    workflow_repo: SQLiteWorkflowRepository | None = None
    jira_gateway: JiraGateway | None = None
    gemini_gateway: GeminiGateway | None = None
    scheduler: AsyncTaskScheduler | None = None
    rules_store: RulesStore | None = None
    user_store: UserStore | None = None
    vpn_manager: NetworkManagerL2tpManager | None = None
    resource_limiter: ResourceLimiter | None = None
    keyed_processor: KeyedProcessor | None = None

    try:
        # Initialize stores & infrastructure
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

        connectivity_service = ConnectivityService(vpn_manager)

        workflow_repo = SQLiteWorkflowRepository(Path(settings.workflow_db_path))
        await workflow_repo.initialize()

        scheduler = AsyncTaskScheduler()
        clock = SystemClock()
        id_generator = UuidIdGenerator()

        jira_gateway = JiraGateway(
            base_url=settings.jira_url,
            verify=settings.jira_verify_ssl,
            timeouts=JiraTimeouts(
                connect=settings.jira_timeout_seconds,
                read=settings.jira_timeout_seconds,
                write=settings.jira_timeout_seconds,
                pool=5.0,
            ),
        )

        gemini_gateway = GeminiGateway(
            api_key=settings.gemini_api_key,
            deadline_seconds=settings.gemini_timeout_seconds,
        )

        # Pure application services
        workflow_service = WorkflowService(
            repository=workflow_repo,
            clock=clock,
            id_generator=id_generator,
        )

        callback_service = CallbackService(
            drafts=workflow_repo,
            tokens=workflow_repo,
            clock=clock,
        )

        submission_service = SubmissionService(
            repository=workflow_repo,
            gateway=jira_gateway,
        )

        attachment_service = AttachmentService(
            repository=workflow_repo,
            gateway=jira_gateway,
            loader=None,
        )

        # Safe metrics & resource limits
        safe_metrics = SafeMetrics()
        resource_limiter = ResourceLimiter(
            {
                ResourceKind.GEMINI: ResourceLimitSpec(
                    global_limit=settings.max_concurrent_gemini,
                    per_actor_limit=min(2, settings.max_concurrent_gemini),
                    queue_limit=settings.max_queue_size,
                    total_deadline_seconds=settings.gemini_timeout_seconds,
                    retry_budget=0,
                    cooldown_failure_threshold=3,
                    cooldown_seconds=5.0,
                ),
                ResourceKind.JIRA: ResourceLimitSpec(
                    global_limit=settings.max_concurrent_jira,
                    per_actor_limit=min(2, settings.max_concurrent_jira),
                    queue_limit=settings.max_queue_size,
                    total_deadline_seconds=10.0,
                    retry_budget=0,
                    cooldown_failure_threshold=3,
                    cooldown_seconds=5.0,
                ),
                ResourceKind.ATTACHMENT: ResourceLimitSpec(
                    global_limit=min(settings.max_concurrent_jira, settings.max_attachment_count),
                    per_actor_limit=min(2, settings.max_concurrent_jira),
                    queue_limit=settings.max_queue_size,
                    total_deadline_seconds=30.0,
                    retry_budget=0,
                    cooldown_failure_threshold=3,
                    cooldown_seconds=5.0,
                ),
            }
        )

        # Build Telegram Application with exact concurrency 1 or KeyedProcessor
        async def post_init(app: Application) -> None:
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
                LOGGER.warning("Could not set bot commands (%s)", type(err).__name__)

        builder = Application.builder().token(settings.telegram_bot_token).post_init(post_init)

        if settings.telegram_concurrent_updates > 1:
            keyed_processor = KeyedProcessor(
                max_concurrency=settings.telegram_concurrent_updates,
                max_queue_size=settings.max_queue_size,
            )
            update_processor = KeyedUpdateProcessor(
                processor=keyed_processor,
                workflow_repo=workflow_repo,
            )
            builder = builder.concurrent_updates(update_processor)
        else:
            builder = builder.concurrent_updates(1)

        application = builder.build()
        application.add_error_handler(handle_application_error)

        application.bot_data["safe_metrics"] = safe_metrics
        application.bot_data["resource_limiter"] = resource_limiter
        if keyed_processor is not None:
            application.bot_data["keyed_processor"] = keyed_processor

        async def on_draft_ready_handler(draft: Draft) -> None:
            try:
                msg = await application.bot.send_message(
                    chat_id=draft.chat_id,
                    text=render_draft_card(draft),
                    parse_mode="HTML",
                    reply_markup=get_draft_reply_keyboard(),
                )
                actions = (
                    CallbackAction.CONFIRM,
                    CallbackAction.TOGGLE_TYPE,
                    CallbackAction.TOGGLE_PRIORITY,
                    CallbackAction.EDIT,
                    CallbackAction.CANCEL,
                )
                issued_buttons = await callback_service.issue_preview_buttons(
                    draft,
                    actions=actions,
                    preview_message_id=msg.message_id,
                )
                await msg.edit_reply_markup(
                    reply_markup=build_draft_inline_keyboard(issued_buttons)
                )
            except Exception as err:
                LOGGER.error("Error sending draft preview to Telegram (%s)", type(err).__name__)

        intake_service = IntakeService(
            repository=workflow_repo,
            analyzer=gemini_gateway,
            rules_repository=rules_store,
            scheduler=scheduler,
            clock=clock,
            id_generator=id_generator,
            default_project_key=settings.jira_default_project_key,
            on_draft_ready=on_draft_ready_handler,
        )

        # Legacy auth/admin handlers compatibility facade
        legacy_jira_client = JiraClient(
            base_url=settings.jira_url,
            verify_ssl=settings.jira_verify_ssl,
            vpn_manager=vpn_manager,
        )
        auth_conv, start_h, logout_h, help_h = build_auth_handlers(
            user_store,
            legacy_jira_client,
            settings.jira_url,
            allowed_user_ids=settings.telegram_allowed_user_ids,
        )
        from telegram.ext import MessageHandler, filters
        logout_btn = MessageHandler(filters.Regex(r"^(🚪 解綁 Jira 帳號|🚪 解绑 Jira 账号)$"), logout_h.callback)
        help_btn = MessageHandler(filters.Regex(r"^(📖 說明|📖 说明)$"), help_h.callback)

        admin_handlers = build_admin_handlers(
            rules_store,
            settings.telegram_admin_user_ids,
            vpn_manager,
        )

        ui_handlers = build_production_ui_handlers(
            workflow_service=workflow_service,
            intake_service=intake_service,
            callback_service=callback_service,
            submission_service=submission_service,
            attachment_service=attachment_service,
            user_store=user_store,
            default_project_key=settings.jira_default_project_key,
            allowed_user_ids=settings.telegram_allowed_user_ids,
        )

        application.add_handlers([
            auth_conv,
            start_h,
            logout_h,
            logout_btn,
            help_h,
            help_btn,
            *admin_handlers,
            *ui_handlers,
        ])

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
        async with application:
            try:
                await updater.start_polling(
                    allowed_updates=("message", "callback_query"),
                    bootstrap_retries=3,
                )
                polling_started = True
                await application.start()
                application_started = True
                LOGGER.info("DZTGBot is running (Phase 7 composition root)")
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
        # Deterministic teardown in reverse initialization order
        if keyed_processor is not None:
            try:
                await keyed_processor.close()
            except Exception as err:
                LOGGER.warning("Error closing keyed processor (%s)", type(err).__name__)

        if resource_limiter is not None:
            try:
                await resource_limiter.close()
            except Exception as err:
                LOGGER.warning("Error closing resource limiter (%s)", type(err).__name__)

        if scheduler is not None:
            try:
                await scheduler.close()
            except Exception as err:
                LOGGER.warning("Error closing task scheduler (%s)", type(err).__name__)

        if gemini_gateway is not None:
            try:
                if hasattr(gemini_gateway, "aclose"):
                    await gemini_gateway.aclose()
            except Exception as err:
                LOGGER.warning("Error closing Gemini gateway (%s)", type(err).__name__)

        if jira_gateway is not None:
            try:
                await jira_gateway.aclose()
            except Exception as err:
                LOGGER.warning("Error closing Jira gateway (%s)", type(err).__name__)

        if workflow_repo is not None:
            try:
                await workflow_repo.close()
            except Exception as err:
                LOGGER.warning("Error closing workflow repository (%s)", type(err).__name__)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOGGER.info("DZTGBot stopped by user.")
    except Exception as error:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        LOGGER.critical("DZTGBot terminated (%s)", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
