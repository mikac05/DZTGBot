"""Phase 7 Task P7-G — abuse-control and isolation gate.

Verifies configured global / per-actor / queue limits cannot be bypassed;
optional allowed-user policy across auth, workflows, callbacks, and admin
gates; malformed/unknown callback isolation (fail-closed, no cross-workflow
blocking or state disclosure); non-sticky cooldown recovery without queue
occupancy leakage.

Deterministic offline fakes only. No live Telegram, Gemini, Jira, or VPN I/O.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.ext import ConversationHandler

from dztgbot.admin import build_admin_handlers
from dztgbot.domain.callbacks import (
    CallbackAction,
    encode_callback_data,
    generate_opaque_token,
    hash_opaque_token,
    parse_callback_data,
)
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate
from dztgbot.domain.policy import (
    DenialCode,
    require_allowed_user,
    require_private_admin,
    user_message_for_denial,
)
from dztgbot.infrastructure.keyed_processor import (
    SAFE_OVERLOAD_FEEDBACK as KP_SAFE_OVERLOAD,
    KeyedProcessor,
    ProcessingOutcome,
    WorkKey,
)
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.jira_auth import AUTH_STARTED_AT_KEY, AWAITING_PAT, build_auth_handlers
from dztgbot.jira_client import JiraUser
from dztgbot.rules import RulesStore
from dztgbot.services.callback_service import CallbackService
from dztgbot.services.limits import (
    SAFE_COOLDOWN_FEEDBACK,
    LimitOutcome,
    ResourceKind,
    ResourceLimitSpec,
    ResourceLimiter,
)
from dztgbot.services.workflow_service import WorkflowService
from dztgbot.ui.handlers.callbacks import handle_callback_query
from dztgbot.ui.handlers.drafts import handle_manual_create
from dztgbot.user_store import UserStore
from dztgbot.vpn import VpnState
from tests.support.security_fakes import TEST_ONLY_PAT


NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)
ALLOWED_ID = 1001
DENIED_ID = 9999
ADMIN_ID = 1001
NOT_ADMIN_ID = 5555
OWNER_ID = 42
CHAT_ID = 42
PREVIEW_ID = 700


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


class SequenceIds:
    def __init__(self) -> None:
        self._n = 0

    def generate_uuid(self) -> str:
        self._n += 1
        return f"abuse-draft-{self._n}"


def _specs(
    *,
    global_limit: int = 2,
    per_actor_limit: int = 1,
    queue_limit: int = 1,
    deadline: float = 1.0,
    retries: int = 0,
    threshold: int = 3,
    cooldown: float = 5.0,
) -> dict[ResourceKind, ResourceLimitSpec]:
    selected = ResourceLimitSpec(
        global_limit=global_limit,
        per_actor_limit=per_actor_limit,
        queue_limit=queue_limit,
        total_deadline_seconds=deadline,
        retry_budget=retries,
        cooldown_failure_threshold=threshold,
        cooldown_seconds=cooldown,
    )
    return {kind: selected for kind in ResourceKind}


def _user(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, full_name="Abuse User", username="abuse_user")


def _private_chat(chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="private", title=None)


def _group_chat(chat_id: int = -99) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="supergroup", title="Ops")


def _message(*, text: str | None = None, message_id: int = 42) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.message_id = message_id
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _update(
    *,
    text: str | None = None,
    user_id: int = ALLOWED_ID,
    chat_id: int | None = None,
    chat_type: str = "private",
    callback_data: str | None = None,
    message_id: int = PREVIEW_ID,
) -> tuple[MagicMock, MagicMock]:
    user = _user(user_id)
    chat = (
        _private_chat(chat_id if chat_id is not None else user_id)
        if chat_type == "private"
        else _group_chat(chat_id if chat_id is not None else -99)
    )
    message = _message(text=text, message_id=message_id)
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    if callback_data is not None:
        query = AsyncMock()
        query.data = callback_data
        query.message = message
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
    else:
        update.callback_query = None
    return update, message


def _context() -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.args = []
    return ctx


class LimitBypassResistance(unittest.IsolatedAsyncioTestCase):
    """Configured global / per-actor / queue bounds cannot be exceeded or raised."""

    async def test_resource_global_per_actor_and_queue_cannot_be_bypassed(self) -> None:
        limiter = ResourceLimiter(
            _specs(global_limit=2, per_actor_limit=1, queue_limit=1, deadline=2.0)
        )
        release = asyncio.Event()
        two_active = asyncio.Event()
        active_global = 0
        max_global = 0
        active_by_actor: dict[int, int] = {}
        max_by_actor: dict[int, int] = {}

        async def work(actor_id: int, _attempt: int) -> int:
            nonlocal active_global, max_global
            active_global += 1
            max_global = max(max_global, active_global)
            active_by_actor[actor_id] = active_by_actor.get(actor_id, 0) + 1
            max_by_actor[actor_id] = max(
                max_by_actor.get(actor_id, 0), active_by_actor[actor_id]
            )
            if active_global >= 2:
                two_active.set()
            await release.wait()
            active_by_actor[actor_id] -= 1
            active_global -= 1
            return actor_id

        # global=2, per_actor=1, queue=1 → capacity admitted = 3.
        # Start two active (actors 1 and 2) + one queued; fourth must overload.
        tasks = [
            asyncio.create_task(
                limiter.run(
                    ResourceKind.GEMINI,
                    actor,
                    lambda attempt, actor=actor: work(actor, attempt),
                )
            )
            for actor in (1, 2, 3)
        ]
        await asyncio.wait_for(two_active.wait(), timeout=0.5)
        # Let the third enter the queue.
        await asyncio.sleep(0)

        overflow = await limiter.try_run(
            ResourceKind.GEMINI,
            4,
            lambda _attempt: asyncio.sleep(0, result="bypass"),
        )
        self.assertEqual(overflow.outcome, LimitOutcome.OVERLOADED)
        self.assertIsNone(overflow.value)
        self.assertEqual(overflow.feedback, "The service is busy. Please try again shortly.")
        # Feedback must not reveal occupancy of other actors' slots.
        self.assertNotIn("1", overflow.feedback or "")
        self.assertNotIn("2", overflow.feedback or "")
        self.assertNotIn("admitted", (overflow.feedback or "").lower())

        release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(sorted(value for value, _ in results), [1, 2, 3])
        self.assertLessEqual(max_global, 2)
        self.assertTrue(all(value <= 1 for value in max_by_actor.values()))

    async def test_caller_cannot_inflate_deadline_or_retry_budget(self) -> None:
        limiter = ResourceLimiter(
            _specs(global_limit=1, per_actor_limit=1, queue_limit=0, deadline=0.5, retries=1)
        )

        async def ok(_attempt: int) -> str:
            return "ok"

        with self.assertRaises(ValueError):
            await limiter.run(
                ResourceKind.JIRA,
                1,
                ok,
                total_deadline_seconds=10.0,  # above configured
            )
        with self.assertRaises(ValueError):
            await limiter.run(
                ResourceKind.JIRA,
                1,
                ok,
                retry_budget=5,  # above configured
            )
        # Within bounds still works.
        value, attempts = await limiter.run(
            ResourceKind.JIRA,
            1,
            ok,
            total_deadline_seconds=0.2,
            retry_budget=0,
        )
        self.assertEqual((value, attempts), ("ok", 1))

    async def test_keyed_processor_capacity_is_hard_bounded(self) -> None:
        processor = KeyedProcessor(max_concurrency=1, max_queue_size=1)
        key_a = WorkKey.for_workflow("capacity-a")
        key_b = WorkKey.for_workflow("capacity-b")
        key_c = WorkKey.for_workflow("capacity-c")
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            started.set()
            await release.wait()

        first = asyncio.create_task(processor.run(key_a, hold))
        await started.wait()
        # One more can queue; third overloads.
        second = asyncio.create_task(
            processor.run(key_b, lambda: asyncio.sleep(0, result="queued"))
        )
        await asyncio.sleep(0)
        third = await processor.try_run(key_c, lambda: asyncio.sleep(0, result="nope"))
        self.assertEqual(third.outcome, ProcessingOutcome.OVERLOADED)
        self.assertEqual(third.feedback, KP_SAFE_OVERLOAD)
        self.assertNotIn("capacity-a", third.feedback or "")
        self.assertNotIn("capacity-b", third.feedback or "")
        release.set()
        await first
        self.assertEqual(await second, "queued")
        await processor.close()


class CooldownRecoveryAndOccupancyPrivacy(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_is_non_sticky_and_hides_queue_occupancy(self) -> None:
        now = [50.0]
        limiter = ResourceLimiter(
            _specs(
                global_limit=1,
                per_actor_limit=1,
                queue_limit=2,
                deadline=1.0,
                threshold=1,
                cooldown=5.0,
                retries=0,
            ),
            monotonic=lambda: now[0],
        )

        async def fail(_attempt: int) -> None:
            raise ConnectionError(
                f"provider exception detail pat={TEST_ONLY_PAT} "
                "https://jira.secret.example/browse/BOT-1 queue depth=99"
            )

        with self.assertRaises(ConnectionError):
            await limiter.run(
                ResourceKind.JIRA,
                7,
                fail,
                retry_if=lambda error: isinstance(error, ConnectionError),
            )

        cooling = await limiter.try_run(
            ResourceKind.JIRA,
            8,
            lambda _attempt: asyncio.sleep(0, result="should-block"),
        )
        self.assertEqual(cooling.outcome, LimitOutcome.COOLDOWN)
        self.assertEqual(cooling.feedback, SAFE_COOLDOWN_FEEDBACK)
        self.assertNotIn("queue", (cooling.feedback or "").lower())
        self.assertNotIn("99", cooling.feedback or "")
        self.assertNotIn("provider", cooling.feedback or "")
        self.assertNotIn(TEST_ONLY_PAT, cooling.feedback or "")
        self.assertNotIn("7", cooling.feedback or "")
        self.assertNotIn("8", cooling.feedback or "")
        snap = await limiter.snapshot(ResourceKind.JIRA)
        self.assertTrue(snap.cooling_down)

        # Advance past cooldown: automatic recovery (non-sticky).
        now[0] += 5.0
        recovered = await limiter.try_run(
            ResourceKind.JIRA,
            8,
            lambda _attempt: asyncio.sleep(0, result="recovered"),
        )
        self.assertEqual(recovered.outcome, LimitOutcome.COMPLETED)
        self.assertEqual(recovered.value, "recovered")
        self.assertFalse((await limiter.snapshot(ResourceKind.JIRA)).cooling_down)

        # A success clears consecutive failures so a single later failure does not
        # immediately re-enter a sticky cooldown when threshold > 1.
        sticky_check = ResourceLimiter(
            _specs(threshold=2, cooldown=10.0, deadline=1.0),
            monotonic=lambda: now[0],
        )
        with self.assertRaises(ConnectionError):
            await sticky_check.run(
                ResourceKind.GEMINI,
                1,
                fail,
                retry_if=lambda error: isinstance(error, ConnectionError),
            )
        # One failure below threshold: still admits work.
        ok = await sticky_check.try_run(
            ResourceKind.GEMINI,
            1,
            lambda _attempt: asyncio.sleep(0, result="still-open"),
        )
        self.assertEqual(ok.outcome, LimitOutcome.COMPLETED)
        self.assertEqual(ok.value, "still-open")


class AllowedUserPolicyEnforcement(unittest.IsolatedAsyncioTestCase):
    """Optional allowlist across auth, workflows, callbacks; admin via admin IDs."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.store = UserStore(base / "creds.json")
        await self.store.initialize()
        self.jira = MagicMock()
        self.jira.validate_credentials = AsyncMock(
            return_value=JiraUser(
                username="jira.user",
                display_name="Jira User",
                email=None,
            )
        )
        self.allowlist = frozenset({ALLOWED_ID})
        (
            self.auth_conv,
            self.start_handler,
            self.logout_handler,
            self.help_handler,
        ) = build_auth_handlers(
            self.store,
            self.jira,
            "https://jira.test.example.com",
            allowed_user_ids=self.allowlist,
        )
        self.auth_entry = self.auth_conv.entry_points[0].callback
        self.not_allowed = user_message_for_denial(DenialCode.NOT_ALLOWED_USER)

        self.repository = SQLiteWorkflowRepository(
            base / "abuse.sqlite3",
            enable_wal=False,
        )
        await self.repository.initialize()
        self.clock = FixedClock()
        self.workflow = WorkflowService(
            repository=self.repository,
            clock=self.clock,
            id_generator=SequenceIds(),
        )
        self.callbacks = CallbackService(
            drafts=self.repository,
            tokens=self.repository,
            clock=self.clock,
        )

        rules_path = base / "rules.txt"
        rules_path.write_text("SECRET_RUNTIME_RULES_BODY\n", encoding="utf-8")
        self.rules_store = RulesStore(rules_path)
        await self.rules_store.initialize()
        self.vpn = MagicMock()
        self.vpn.status = AsyncMock(
            return_value=SimpleNamespace(
                state=VpnState.UP if hasattr(VpnState, "UP") else "up",
                message="VPN is up (endpoint=vpn.secret.example)",
            )
        )
        self.vpn.start = AsyncMock(
            return_value=SimpleNamespace(
                state=VpnState.UP if hasattr(VpnState, "UP") else "up",
                message="VPN start accepted",
            )
        )
        self.admin_handlers = {
            next(iter(handler.commands)): handler.callback  # type: ignore[attr-defined]
            for handler in build_admin_handlers(
                self.rules_store,
                frozenset({ADMIN_ID}),
                self.vpn,
            )
        }

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self._tmp.cleanup()

    def _assert_allowlist_denial(self, body: str) -> None:
        self.assertEqual(body, self.not_allowed)
        self.assertNotIn(TEST_ONLY_PAT, body)
        self.assertNotIn(str(ALLOWED_ID), body)
        self.assertNotIn(str(DENIED_ID), body)
        self.assertNotIn("SECRET_RUNTIME_RULES", body)

    async def test_policy_layer_allowlist_contract(self) -> None:
        self.assertTrue(require_allowed_user(ALLOWED_ID, None).allowed)
        self.assertTrue(require_allowed_user(DENIED_ID, frozenset()).allowed)
        self.assertTrue(require_allowed_user(ALLOWED_ID, self.allowlist).allowed)
        denied = require_allowed_user(DENIED_ID, self.allowlist)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.denial_code, DenialCode.NOT_ALLOWED_USER)

    async def test_auth_handlers_enforce_allowlist(self) -> None:
        # Allowed may start auth.
        update_ok, message_ok = _update(text="/auth", user_id=ALLOWED_ID)
        result_ok = await self.auth_entry(update_ok, _context())
        self.assertEqual(result_ok, AWAITING_PAT)
        self.assertIn("PAT", message_ok.reply_text.await_args.args[0])

        # Denied is rejected with fixed copy and no conversation state.
        update_no, message_no = _update(text="/auth", user_id=DENIED_ID)
        ctx_no = _context()
        result_no = await self.auth_entry(update_no, ctx_no)
        self.assertEqual(result_no, ConversationHandler.END)
        self.assertNotIn(AUTH_STARTED_AT_KEY, ctx_no.user_data)
        self._assert_allowlist_denial(message_no.reply_text.await_args.args[0])

        # Start / help / logout also deny without leaking auth state.
        for handler in (
            self.start_handler.callback,
            self.help_handler.callback,
            self.logout_handler.callback,
        ):
            update, message = _update(text="/x", user_id=DENIED_ID)
            await handler(update, _context())
            self._assert_allowlist_denial(message.reply_text.await_args.args[0])

    async def test_workflow_and_callback_handlers_enforce_allowlist(self) -> None:
        # Manual create denied for non-allowlisted actor.
        update_denied, msg_denied = _update(text="/new bug", user_id=DENIED_ID)
        ctx = _context()
        ctx.args = ["bug"]
        await handle_manual_create(
            update_denied,
            ctx,
            workflow_service=self.workflow,
            callback_service=self.callbacks,
            allowed_user_ids=self.allowlist,
        )
        self.assertIsNone(ctx.user_data.get("active_draft_id"))
        msg_denied.reply_html.assert_awaited()
        body = msg_denied.reply_html.await_args.args[0]
        self.assertIn(self.not_allowed, body)
        self.assertNotIn(TEST_ONLY_PAT, body)

        # Allowed actor can create a draft for callback issuance.
        draft = await self.workflow.create_manual_draft(
            owner_id=ALLOWED_ID,
            chat_id=ALLOWED_ID,
            template=JiraTaskTemplate("BOT", "Task", "allowlist summary", "desc", "Medium"),
        )
        issued = await self.callbacks.issue_preview_buttons(
            draft,
            actions=(CallbackAction.CANCEL,),
            preview_message_id=PREVIEW_ID,
        )
        callback_data = issued[CallbackAction.CANCEL].callback_data

        # Callback denied for foreign non-allowlisted actor.
        update_cb, _ = _update(
            user_id=DENIED_ID,
            chat_id=ALLOWED_ID,
            callback_data=callback_data,
            message_id=PREVIEW_ID,
        )
        await handle_callback_query(
            update_cb,
            _context(),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
            allowed_user_ids=self.allowlist,
        )
        update_cb.callback_query.answer.assert_awaited()
        answer = update_cb.callback_query.answer.await_args.args[0]
        self.assertEqual(answer, self.not_allowed)
        # Draft must remain untouched.
        final = await self.repository.get_by_id(draft.draft_id)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.state, draft.state)
        self.assertEqual(final.revision, draft.revision)

    async def test_admin_commands_use_admin_id_gate_as_intended(self) -> None:
        """Administrator commands are gated by admin numeric IDs (not allowlist alone)."""

        # Non-admin private user cannot read rules (even if they would be "allowed users").
        decision = require_private_admin("private", NOT_ADMIN_ID, frozenset({ADMIN_ID}))
        self.assertEqual(decision.denial_code, DenialCode.NOT_ADMIN)

        update, message = _update(text="/rules", user_id=NOT_ADMIN_ID)
        await self.admin_handlers["rules"](update, _context())
        message.reply_text.assert_awaited()
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_ADMIN))
        self.assertNotIn("SECRET_RUNTIME_RULES", body)
        self.assertNotIn("vpn.secret.example", body)
        self.vpn.status.assert_not_awaited()

        # Group chat never discloses rules / VPN / admin membership.
        group_update, group_message = _update(
            text="/vpn",
            user_id=ADMIN_ID,
            chat_type="supergroup",
            chat_id=-5001,
        )
        await self.admin_handlers["vpn"](group_update, _context())
        group_body = group_message.reply_text.await_args.args[0]
        self.assertEqual(group_body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("SECRET_RUNTIME_RULES", group_body)
        self.assertNotIn("vpn.secret.example", group_body)
        self.vpn.status.assert_not_awaited()

        # Authorised private admin may proceed without leaking credential paths.
        admin_update, admin_message = _update(text="/rules", user_id=ADMIN_ID)
        await self.admin_handlers["rules"](admin_update, _context())
        admin_body = admin_message.reply_text.await_args.args[0]
        self.assertIn("SECRET_RUNTIME_RULES", admin_body)
        self.assertNotIn(TEST_ONLY_PAT, admin_body)
        self.assertNotIn("USER_CREDENTIALS_PATH", admin_body)


class CallbackKeyIsolation(unittest.IsolatedAsyncioTestCase):
    """Malformed / unknown callback keys fail closed without cross-workflow effects."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(
            Path(self._tmp.name) / "iso.sqlite3",
            enable_wal=False,
        )
        await self.repository.initialize()
        self.clock = FixedClock()
        self.workflow = WorkflowService(
            repository=self.repository,
            clock=self.clock,
            id_generator=SequenceIds(),
        )
        self.callbacks = CallbackService(
            drafts=self.repository,
            tokens=self.repository,
            clock=self.clock,
        )

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self._tmp.cleanup()

    async def _issue_two_drafts(self) -> tuple[Draft, str, Draft, str]:
        draft_a = await self.workflow.create_manual_draft(
            owner_id=OWNER_ID,
            chat_id=CHAT_ID,
            template=JiraTaskTemplate("BOT", "Task", "victim summary", "desc-a", "Medium"),
        )
        draft_b = await self.workflow.create_manual_draft(
            owner_id=OWNER_ID + 1,
            chat_id=CHAT_ID + 1,
            template=JiraTaskTemplate("BOT", "Task", "other summary", "desc-b", "Low"),
        )
        issued_a = await self.callbacks.issue_preview_buttons(
            draft_a,
            actions=(CallbackAction.CANCEL,),
            preview_message_id=PREVIEW_ID,
        )
        issued_b = await self.callbacks.issue_preview_buttons(
            draft_b,
            actions=(CallbackAction.CANCEL,),
            preview_message_id=PREVIEW_ID + 1,
        )
        return (
            draft_a,
            issued_a[CallbackAction.CANCEL].callback_data,
            draft_b,
            issued_b[CallbackAction.CANCEL].callback_data,
        )

    async def test_malformed_and_unknown_callbacks_fail_closed_without_disclosure(self) -> None:
        draft_a, good_a, draft_b, _good_b = await self._issue_two_drafts()
        state_a_before = draft_a.state
        rev_a_before = draft_a.revision
        state_b_before = draft_b.state
        rev_b_before = draft_b.revision

        malformed_payloads = (
            "jira_confirm",
            "j1:cfm",
            "j1:cfm:not-hex",
            "j1:ZZZ:" + "ab" * 16,
            "j1:cfm:" + "deadbeef" * 4 + ":EXTRA",
            "j1:cfm:" + "00" * 16 + f":{TEST_ONLY_PAT}",
            "",
            None,
        )
        for payload in malformed_payloads:
            result = await self.callbacks.authorize(
                raw_callback_data=payload,  # type: ignore[arg-type]
                actor_user_id=OWNER_ID,
                chat_id=CHAT_ID,
                chat_type="private",
                preview_message_id=PREVIEW_ID,
            )
            self.assertFalse(result.allowed)
            self.assertEqual(result.denial_code, DenialCode.MALFORMED_CALLBACK)
            self.assertIsNone(result.draft)
            msg = result.user_message or ""
            self.assertEqual(msg, user_message_for_denial(DenialCode.MALFORMED_CALLBACK))
            self.assertNotIn(TEST_ONLY_PAT, msg)
            if isinstance(payload, str) and payload:
                self.assertNotIn(payload, msg)

        # Well-formed but unknown token: fail closed, no draft disclosure.
        unknown = encode_callback_data(
            CallbackAction.CONFIRM,
            generate_opaque_token(),
        )
        unknown_result = await self.callbacks.authorize(
            raw_callback_data=unknown,
            actor_user_id=OWNER_ID,
            chat_id=CHAT_ID,
            chat_type="private",
            preview_message_id=PREVIEW_ID,
        )
        self.assertFalse(unknown_result.allowed)
        self.assertEqual(unknown_result.denial_code, DenialCode.UNKNOWN_TOKEN)
        self.assertIsNone(unknown_result.draft)
        self.assertEqual(
            unknown_result.user_message,
            user_message_for_denial(DenialCode.UNKNOWN_TOKEN),
        )
        # Token hash may be present for diagnostics but user message must not echo it.
        if unknown_result.token_hash is not None:
            self.assertNotIn(unknown_result.token_hash, unknown_result.user_message or "")
            self.assertNotIn(unknown.split(":")[-1], unknown_result.user_message or "")

        # Neither legitimate draft mutated by attack traffic.
        after_a = await self.repository.get_by_id(draft_a.draft_id)
        after_b = await self.repository.get_by_id(draft_b.draft_id)
        assert after_a is not None and after_b is not None
        self.assertEqual((after_a.state, after_a.revision), (state_a_before, rev_a_before))
        self.assertEqual((after_b.state, after_b.revision), (state_b_before, rev_b_before))

        # Legitimate cancel on A still works (isolation from attack noise).
        ok = await self.callbacks.authorize(
            raw_callback_data=good_a,
            actor_user_id=OWNER_ID,
            chat_id=CHAT_ID,
            chat_type="private",
            preview_message_id=PREVIEW_ID,
        )
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.action, CallbackAction.CANCEL)
        self.assertIsNotNone(ok.draft)
        assert ok.draft is not None
        self.assertEqual(ok.draft.draft_id, draft_a.draft_id)

    async def test_attacker_controlled_work_keys_cannot_cross_block_workflows(self) -> None:
        """Serialization keys are opaque; foreign keys do not stall unrelated work."""

        processor = KeyedProcessor(max_concurrency=2, max_queue_size=4)
        victim_key = WorkKey.for_workflow("victim-draft-id")
        # Attacker uses their own collection scope — must not equal victim workflow key.
        attacker_key = WorkKey.for_collection(
            actor_id=9999,
            chat_id=9999,
            message_thread_id=None,
        )
        self.assertNotEqual(victim_key, attacker_key)

        # Unsupported / injected namespaces fail closed at construction.
        with self.assertRaises(ValueError):
            WorkKey(namespace="admin", _digest=b"\x00" * 16)
        with self.assertRaises(ValueError):
            WorkKey(namespace="workflow", _digest=b"\x00" * 8)
        with self.assertRaises(TypeError):
            await processor.run("raw-attacker-string-key", lambda: asyncio.sleep(0))  # type: ignore[arg-type]

        victim_started = asyncio.Event()
        release_victim = asyncio.Event()
        attacker_started = asyncio.Event()
        release_attacker = asyncio.Event()

        async def victim_op() -> str:
            victim_started.set()
            await release_victim.wait()
            return "victim-done"

        async def attacker_op() -> str:
            attacker_started.set()
            await release_attacker.wait()
            return "attacker-done"

        victim_task = asyncio.create_task(processor.run(victim_key, victim_op))
        attacker_task = asyncio.create_task(processor.run(attacker_key, attacker_op))
        await asyncio.wait_for(victim_started.wait(), timeout=0.5)
        await asyncio.wait_for(attacker_started.wait(), timeout=0.5)
        # Both progress independently while the other is held.
        self.assertFalse(victim_task.done())
        self.assertFalse(attacker_task.done())

        release_attacker.set()
        self.assertEqual(await asyncio.wait_for(attacker_task, timeout=0.5), "attacker-done")
        self.assertFalse(victim_task.done())
        release_victim.set()
        self.assertEqual(await victim_task, "victim-done")

        # Overload on one key still returns fixed copy with no peer-key identity.
        tight = KeyedProcessor(max_concurrency=1, max_queue_size=0)
        hold_started = asyncio.Event()
        hold_release = asyncio.Event()

        async def hold() -> None:
            hold_started.set()
            await hold_release.wait()

        hold_task = asyncio.create_task(tight.run(victim_key, hold))
        await hold_started.wait()
        blocked = await tight.try_run(attacker_key, lambda: asyncio.sleep(0))
        self.assertEqual(blocked.outcome, ProcessingOutcome.OVERLOADED)
        self.assertEqual(blocked.feedback, KP_SAFE_OVERLOAD)
        self.assertNotIn("victim", blocked.feedback or "")
        self.assertNotIn("attacker", blocked.feedback or "")
        self.assertNotIn("9999", blocked.feedback or "")
        hold_release.set()
        await hold_task
        await tight.close()
        await processor.close()

    async def test_handler_malformed_callback_does_not_mutate_peer_workflow(self) -> None:
        draft_a, _good_a, draft_b, good_b = await self._issue_two_drafts()

        evil = "j1:cfm:" + "ff" * 16 + f":{TEST_ONLY_PAT}"
        update_evil, _ = _update(
            user_id=OWNER_ID,
            chat_id=CHAT_ID,
            callback_data=evil,
            message_id=PREVIEW_ID,
        )
        await handle_callback_query(
            update_evil,
            _context(),
            callback_service=self.callbacks,
            workflow_service=self.workflow,
        )
        update_evil.callback_query.answer.assert_awaited()
        feedback = update_evil.callback_query.answer.await_args.args[0]
        self.assertEqual(feedback, user_message_for_denial(DenialCode.MALFORMED_CALLBACK))
        self.assertNotIn(TEST_ONLY_PAT, feedback)
        self.assertNotIn(evil, feedback)

        # Peer workflow B still authorizes cleanly.
        ok_b = await self.callbacks.authorize(
            raw_callback_data=good_b,
            actor_user_id=OWNER_ID + 1,
            chat_id=CHAT_ID + 1,
            chat_type="private",
            preview_message_id=PREVIEW_ID + 1,
        )
        self.assertTrue(ok_b.allowed)
        after_a = await self.repository.get_by_id(draft_a.draft_id)
        assert after_a is not None
        self.assertEqual(after_a.state, draft_a.state)


class RateLimitResponsePrivacy(unittest.IsolatedAsyncioTestCase):
    """Queue/rate-limit control results never reveal peer workflow occupancy."""

    async def test_overload_results_are_uniform_across_actors_and_keys(self) -> None:
        limiter = ResourceLimiter(
            _specs(global_limit=1, per_actor_limit=1, queue_limit=0, deadline=1.0)
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(_attempt: int) -> None:
            started.set()
            await release.wait()

        hold_task = asyncio.create_task(limiter.run(ResourceKind.GEMINI, 1, hold))
        await started.wait()

        results = []
        for actor in (2, 3, 4):
            result = await limiter.try_run(
                ResourceKind.GEMINI,
                actor,
                lambda _attempt: asyncio.sleep(0, result="x"),
            )
            results.append(result)

        # Uniform fixed outcome/copy — no per-actor differentiation that would
        # disclose which peer is holding the slot.
        for result in results:
            self.assertEqual(result.outcome, LimitOutcome.OVERLOADED)
            self.assertEqual(result.feedback, results[0].feedback)
            self.assertIsNone(result.value)
            self.assertNotIn(str(actor), result.feedback or "")

        release.set()
        await hold_task

        processor = KeyedProcessor(max_concurrency=1, max_queue_size=0)
        p_started = asyncio.Event()
        p_release = asyncio.Event()

        async def p_hold() -> None:
            p_started.set()
            await p_release.wait()

        p_task = asyncio.create_task(
            processor.run(WorkKey.for_workflow("holder"), p_hold)
        )
        await p_started.wait()
        for draft_id in ("peer-1", "peer-2", "peer-secret-summary"):
            result = await processor.try_run(
                WorkKey.for_workflow(draft_id),
                lambda: asyncio.sleep(0),
            )
            self.assertEqual(result.outcome, ProcessingOutcome.OVERLOADED)
            self.assertEqual(result.feedback, KP_SAFE_OVERLOAD)
            self.assertNotIn(draft_id, result.feedback or "")
            self.assertNotIn("holder", result.feedback or "")
        p_release.set()
        await p_task
        await processor.close()


class CallbackGrammarPossessionAloneIsInsufficient(unittest.TestCase):
    def test_parse_success_does_not_imply_authorization(self) -> None:
        token = generate_opaque_token()
        wire = encode_callback_data(CallbackAction.CONFIRM, token)
        parsed = parse_callback_data(wire)
        self.assertEqual(parsed.action, CallbackAction.CONFIRM)
        self.assertEqual(parsed.opaque_token, token)
        # Hash is storage form only — not an authorization decision.
        digest = hash_opaque_token(token)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, token)


if __name__ == "__main__":
    unittest.main()
