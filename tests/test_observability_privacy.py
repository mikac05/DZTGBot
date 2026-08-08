"""Phase 7 Task P7-G — observability privacy gate.

Verifies that overload / deadline / cooldown / queue / rate-limit control
surfaces and SafeMetrics / log fields never disclose other workflow or user
state, raw Telegram identifiers, callback tokens, file IDs, message content,
PATs, Jira bodies/URLs, provider exception text, VPN details, or credential
paths. Opaque correlation IDs and fixed safe codes/copy only.

Deterministic offline fakes only. No live Telegram, Gemini, Jira, or VPN I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dztgbot.__main__ import handle_application_error
from dztgbot.domain.callbacks import CallbackParseError, hash_opaque_token, parse_callback_data
from dztgbot.domain.policy import DenialCode, user_message_for_denial
from dztgbot.infrastructure.keyed_processor import (
    SAFE_CLOSED_FEEDBACK as KP_SAFE_CLOSED,
    SAFE_DEADLINE_FEEDBACK as KP_SAFE_DEADLINE,
    SAFE_OVERLOAD_FEEDBACK as KP_SAFE_OVERLOAD,
    KeyedProcessor,
    ProcessingOutcome,
    ProcessorClosedError,
    ProcessorDeadlineExceededError,
    ProcessorOverloadedError,
    WorkKey,
)
from dztgbot.services.limits import (
    SAFE_CLOSED_FEEDBACK as RL_SAFE_CLOSED,
    SAFE_COOLDOWN_FEEDBACK,
    SAFE_DEADLINE_FEEDBACK as RL_SAFE_DEADLINE,
    SAFE_OVERLOAD_FEEDBACK as RL_SAFE_OVERLOAD,
    LimitOutcome,
    ResourceKind,
    ResourceLimitSpec,
    ResourceLimiter,
    ResourceCooldownError,
    ResourceDeadlineExceededError,
    ResourceLimiterClosedError,
    ResourceOverloadedError,
)
from dztgbot.services.observability import (
    CorrelationId,
    EventCode,
    OutcomeCode,
    SafeMetrics,
    new_correlation_id,
)
from dztgbot.user_store import JiraCredentials
from tests.support.security_fakes import TEST_ONLY_PAT


# Synthetic secrets / identifiers that must never appear on control surfaces.
SENSITIVE_MARKERS = (
    TEST_ONLY_PAT,
    "Bearer ",
    "1234567890",  # raw Telegram-style numeric id
    "chat_id=",
    "user_id=",
    "message_id=",
    "file_id=",
    "AgADBAADSECRET_FILE_ID",
    "j1:cfm:",
    "deadbeef" * 4,  # token hex shape
    "https://jira.secret.example/browse/BOT-99",
    "Jira body: field 'summary' rejected",
    "provider exception detail",
    "vpn.secret.example",
    "/etc/NetworkManager/system-connections/secret.nmconnection",
    "USER_CREDENTIALS_PATH",
    "forwarded private message body",
    "template summary SECRET",
    "callback token hash",
)


FIXED_PROCESSOR_FEEDBACK = frozenset(
    {
        KP_SAFE_OVERLOAD,
        KP_SAFE_DEADLINE,
        KP_SAFE_CLOSED,
    }
)
FIXED_LIMITER_FEEDBACK = frozenset(
    {
        RL_SAFE_OVERLOAD,
        RL_SAFE_DEADLINE,
        SAFE_COOLDOWN_FEEDBACK,
        RL_SAFE_CLOSED,
    }
)
FIXED_PROCESSOR_CODES = frozenset(
    {
        "processor_overloaded",
        "processor_deadline_exceeded",
        "processor_closed",
    }
)
FIXED_LIMITER_CODES = frozenset(
    {
        "resource_overloaded",
        "resource_deadline_exceeded",
        "resource_cooldown",
        "resource_limiter_closed",
    }
)

_CORRELATION_PATTERN = re.compile(r"^c1_[A-Za-z0-9_-]{16,64}$")


def _assert_no_sensitive(test: unittest.TestCase, surface: str, *, label: str) -> None:
    for marker in SENSITIVE_MARKERS:
        test.assertNotIn(
            marker,
            surface,
            msg=f"{label} must not contain sensitive marker {marker!r}: {surface!r}",
        )


def _limiter_specs(
    *,
    global_limit: int = 1,
    per_actor_limit: int = 1,
    queue_limit: int = 0,
    deadline: float = 0.05,
    threshold: int = 1,
    cooldown: float = 5.0,
    retries: int = 0,
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


class ControlSurfaceFeedbackPrivacy(unittest.IsolatedAsyncioTestCase):
    """Overload / deadline / cooldown / queue responses use only fixed safe copy."""

    async def test_keyed_processor_control_feedback_is_fixed_and_state_free(self) -> None:
        victim_draft = "draft-victim-SECRET-template-summary"
        attacker_draft = "draft-attacker-forwarded-private-message-body"
        processor = KeyedProcessor(max_concurrency=1, max_queue_size=0)
        victim_key = WorkKey.for_workflow(victim_draft)
        attacker_key = WorkKey.for_workflow(attacker_draft)

        started = asyncio.Event()
        release = asyncio.Event()

        async def hold_victim() -> str:
            started.set()
            await release.wait()
            return "victim-ok"

        hold = asyncio.create_task(processor.run(victim_key, hold_victim))
        await started.wait()

        # Capacity exhausted by victim work: attacker sees only fixed overload copy.
        overloaded = await processor.try_run(
            attacker_key,
            lambda: asyncio.sleep(0, result="must-not-run"),
        )
        self.assertEqual(overloaded.outcome, ProcessingOutcome.OVERLOADED)
        self.assertEqual(overloaded.feedback, KP_SAFE_OVERLOAD)
        self.assertIsNone(overloaded.value)
        self.assertIn(overloaded.feedback, FIXED_PROCESSOR_FEEDBACK)
        _assert_no_sensitive(self, overloaded.feedback or "", label="processor overload feedback")
        _assert_no_sensitive(self, str(overloaded), label="processor overload result")
        self.assertNotIn(victim_draft, str(overloaded))
        self.assertNotIn(attacker_draft, str(overloaded))

        with self.assertRaises(ProcessorOverloadedError) as overload_exc:
            await processor.run(attacker_key, lambda: asyncio.sleep(0))
        self.assertEqual(str(overload_exc.exception), "processor_overloaded")
        self.assertEqual(overload_exc.exception.code, "processor_overloaded")
        self.assertEqual(overload_exc.exception.feedback, KP_SAFE_OVERLOAD)
        self.assertIn(overload_exc.exception.code, FIXED_PROCESSOR_CODES)
        _assert_no_sensitive(self, str(overload_exc.exception), label="ProcessorOverloadedError")

        # Deadline path: fixed copy only; no queue occupancy disclosure.
        deadline_result = await processor.try_run(
            attacker_key,
            lambda: asyncio.sleep(1),
            total_deadline_seconds=0.01,
        )
        # With capacity full this is OVERLOADED (admission) rather than deadline —
        # either fixed control outcome is acceptable, never a leaky one.
        self.assertIn(
            deadline_result.outcome,
            {ProcessingOutcome.OVERLOADED, ProcessingOutcome.DEADLINE_EXCEEDED},
        )
        self.assertIn(deadline_result.feedback, FIXED_PROCESSOR_FEEDBACK)
        _assert_no_sensitive(self, deadline_result.feedback or "", label="processor deadline/overload")

        release.set()
        self.assertEqual(await hold, "victim-ok")

        # After free capacity, deadline still uses fixed feedback.
        timed_out = await processor.try_run(
            WorkKey.for_workflow("deadline-only"),
            lambda: asyncio.sleep(1),
            total_deadline_seconds=0.01,
        )
        self.assertEqual(timed_out.outcome, ProcessingOutcome.DEADLINE_EXCEEDED)
        self.assertEqual(timed_out.feedback, KP_SAFE_DEADLINE)
        self.assertEqual(str(ProcessorDeadlineExceededError()), "processor_deadline_exceeded")
        _assert_no_sensitive(self, timed_out.feedback or "", label="processor deadline feedback")

        await processor.close()
        closed = await processor.try_run(
            WorkKey.for_workflow("after-close"),
            lambda: asyncio.sleep(0, result="x"),
        )
        self.assertEqual(closed.outcome, ProcessingOutcome.CLOSED)
        self.assertEqual(closed.feedback, KP_SAFE_CLOSED)
        self.assertEqual(str(ProcessorClosedError()), "processor_closed")
        _assert_no_sensitive(self, closed.feedback or "", label="processor closed feedback")

    async def test_resource_limiter_control_feedback_is_fixed_and_state_free(self) -> None:
        limiter = ResourceLimiter(
            _limiter_specs(global_limit=1, per_actor_limit=1, queue_limit=0, deadline=0.05)
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(_attempt: int) -> str:
            started.set()
            await release.wait()
            return "held"

        hold_task = asyncio.create_task(limiter.run(ResourceKind.GEMINI, 111, hold))
        await started.wait()

        overloaded = await limiter.try_run(
            ResourceKind.GEMINI,
            222,
            lambda _attempt: asyncio.sleep(0, result="must-not-run"),
        )
        self.assertEqual(overloaded.outcome, LimitOutcome.OVERLOADED)
        self.assertEqual(overloaded.feedback, RL_SAFE_OVERLOAD)
        self.assertIsNone(overloaded.value)
        self.assertIn(overloaded.feedback, FIXED_LIMITER_FEEDBACK)
        _assert_no_sensitive(self, overloaded.feedback or "", label="limiter overload feedback")
        # Must not reveal other actor identity or queue occupancy details.
        self.assertNotIn("111", overloaded.feedback or "")
        self.assertNotIn("222", overloaded.feedback or "")
        self.assertNotIn("admitted", (overloaded.feedback or "").lower())
        self.assertNotIn("queue", (overloaded.feedback or "").lower())
        self.assertNotIn("active", (overloaded.feedback or "").lower())

        with self.assertRaises(ResourceOverloadedError) as exc:
            await limiter.run(ResourceKind.GEMINI, 333, lambda _a: asyncio.sleep(0))
        self.assertEqual(str(exc.exception), "resource_overloaded")
        self.assertEqual(exc.exception.code, "resource_overloaded")
        self.assertEqual(exc.exception.feedback, RL_SAFE_OVERLOAD)
        self.assertIn(exc.exception.code, FIXED_LIMITER_CODES)
        _assert_no_sensitive(self, str(exc.exception), label="ResourceOverloadedError")

        release.set()
        await hold_task

        timed_out = await limiter.try_run(
            ResourceKind.GEMINI,
            444,
            lambda _attempt: asyncio.sleep(1),
            total_deadline_seconds=0.01,
        )
        self.assertEqual(timed_out.outcome, LimitOutcome.DEADLINE_EXCEEDED)
        self.assertEqual(timed_out.feedback, RL_SAFE_DEADLINE)
        self.assertEqual(
            str(ResourceDeadlineExceededError()), "resource_deadline_exceeded"
        )
        _assert_no_sensitive(self, timed_out.feedback or "", label="limiter deadline feedback")

        # Cooldown: consecutive retryable failures → fixed cooldown copy.
        cooling_limiter = ResourceLimiter(
            _limiter_specs(threshold=1, cooldown=30.0, deadline=1.0, queue_limit=2),
            monotonic=lambda: 100.0,
        )

        async def fail(_attempt: int) -> None:
            raise ConnectionError(
                "provider exception detail Jira body: field rejected "
                f"pat={TEST_ONLY_PAT} https://jira.secret.example/browse/BOT-99"
            )

        with self.assertRaises(ConnectionError):
            await cooling_limiter.run(
                ResourceKind.JIRA,
                1,
                fail,
                retry_if=lambda error: isinstance(error, ConnectionError),
            )
        cooling = await cooling_limiter.try_run(
            ResourceKind.JIRA,
            2,
            lambda _attempt: asyncio.sleep(0, result="blocked"),
        )
        self.assertEqual(cooling.outcome, LimitOutcome.COOLDOWN)
        self.assertEqual(cooling.feedback, SAFE_COOLDOWN_FEEDBACK)
        self.assertEqual(str(ResourceCooldownError()), "resource_cooldown")
        _assert_no_sensitive(self, cooling.feedback or "", label="cooldown feedback")
        self.assertNotIn("provider", cooling.feedback or "")
        self.assertNotIn("Jira", cooling.feedback or "")
        self.assertNotIn(TEST_ONLY_PAT, cooling.feedback or "")

        await cooling_limiter.close()
        closed = await cooling_limiter.try_run(
            ResourceKind.GEMINI, 9, lambda _a: asyncio.sleep(0)
        )
        self.assertEqual(closed.outcome, LimitOutcome.CLOSED)
        self.assertEqual(closed.feedback, RL_SAFE_CLOSED)
        self.assertEqual(str(ResourceLimiterClosedError()), "resource_limiter_closed")
        _assert_no_sensitive(self, closed.feedback or "", label="limiter closed feedback")

    async def test_control_exceptions_do_not_embed_operation_or_provider_text(self) -> None:
        """Fixed-code exceptions carry only their code string, not chained detail."""

        for exc_type, code in (
            (ProcessorOverloadedError, "processor_overloaded"),
            (ProcessorDeadlineExceededError, "processor_deadline_exceeded"),
            (ProcessorClosedError, "processor_closed"),
            (ResourceOverloadedError, "resource_overloaded"),
            (ResourceDeadlineExceededError, "resource_deadline_exceeded"),
            (ResourceCooldownError, "resource_cooldown"),
            (ResourceLimiterClosedError, "resource_limiter_closed"),
        ):
            with self.subTest(exc_type=exc_type.__name__):
                error = exc_type()
                self.assertEqual(str(error), code)
                self.assertEqual(error.args, (code,))
                _assert_no_sensitive(self, repr(error), label=exc_type.__name__)


class SafeMetricsPrivacy(unittest.TestCase):
    """SafeMetrics accepts only fixed enums + opaque correlation IDs."""

    def test_record_api_rejects_raw_identifiers_and_freeform_labels(self) -> None:
        metrics = SafeMetrics(recent_event_limit=8)
        correlation = new_correlation_id()

        # Signature has no actor/chat/token/label parameters.
        params = inspect.signature(SafeMetrics.record).parameters
        forbidden = {
            "actor_id",
            "user_id",
            "chat_id",
            "message_id",
            "file_id",
            "token",
            "token_hash",
            "labels",
            "tags",
            "pat",
            "url",
            "body",
            "text",
            "content",
            "path",
        }
        self.assertTrue(forbidden.isdisjoint(params.keys()))

        metrics.record(EventCode.KEYED_PROCESS, OutcomeCode.OK, correlation, duration_seconds=0.01)
        metrics.record(EventCode.GEMINI_CALL, OutcomeCode.OVERLOADED, correlation)
        metrics.record(EventCode.JIRA_CALL, OutcomeCode.DEADLINE, correlation)
        metrics.record(EventCode.ATTACHMENT_CALL, OutcomeCode.COOLDOWN, correlation)
        metrics.record(EventCode.QUEUE_ADMISSION, OutcomeCode.ERROR, correlation)
        metrics.record(EventCode.SHUTDOWN, OutcomeCode.CANCELLED, correlation)

        with self.assertRaises(TypeError):
            metrics.record("jira_call", OutcomeCode.OK, correlation)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            metrics.record(EventCode.JIRA_CALL, "ok", correlation)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            metrics.record(EventCode.JIRA_CALL, OutcomeCode.OK, "c1_not_a_real_object")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            CorrelationId("raw-user-id-1234567890")
        with self.assertRaises(ValueError):
            CorrelationId(TEST_ONLY_PAT)
        with self.assertRaises(ValueError):
            CorrelationId("j1:cfm:deadbeefdeadbeefdeadbeefdeadbeef")

    def test_snapshot_fields_exclude_sensitive_material(self) -> None:
        metrics = SafeMetrics(recent_event_limit=16)
        # Correlation IDs are opaque; ensure generated values match the grammar.
        correlations = [new_correlation_id() for _ in range(4)]
        outcomes = (
            OutcomeCode.OK,
            OutcomeCode.OVERLOADED,
            OutcomeCode.DEADLINE,
            OutcomeCode.COOLDOWN,
        )
        events = (
            EventCode.KEYED_PROCESS,
            EventCode.GEMINI_CALL,
            EventCode.JIRA_CALL,
            EventCode.QUEUE_ADMISSION,
        )
        for event, outcome, correlation in zip(events, outcomes, correlations, strict=True):
            metrics.record(event, outcome, correlation, duration_seconds=0.002)

        snapshot = metrics.snapshot()
        for key in snapshot.counters:
            event, outcome = key
            self.assertIsInstance(event, EventCode)
            self.assertIsInstance(outcome, OutcomeCode)
            self.assertIn(event, EventCode)
            self.assertIn(outcome, OutcomeCode)

        for observation in snapshot.recent:
            self.assertIsInstance(observation.event, EventCode)
            self.assertIsInstance(observation.outcome, OutcomeCode)
            self.assertIsInstance(observation.correlation_id, CorrelationId)
            self.assertRegex(observation.correlation_id.value, _CORRELATION_PATTERN)
            # Opaque repr must not echo the raw token value.
            self.assertNotIn(observation.correlation_id.value, repr(observation.correlation_id))
            self.assertEqual(repr(observation.correlation_id), "CorrelationId(<opaque>)")
            _assert_no_sensitive(self, repr(observation), label="Observation repr")
            _assert_no_sensitive(self, observation.event.value, label="event code")
            _assert_no_sensitive(self, observation.outcome.value, label="outcome code")
            # Correlation value itself must not look like Telegram IDs / tokens / paths.
            self.assertFalse(observation.correlation_id.value.isdigit())
            self.assertNotIn("j1:", observation.correlation_id.value)
            self.assertNotIn("/", observation.correlation_id.value)
            self.assertNotIn(TEST_ONLY_PAT, observation.correlation_id.value)

        # Aggregate dump must remain free of the synthetic secret markers.
        dump = repr(snapshot.counters) + repr(snapshot.total_duration_ms) + repr(snapshot.recent)
        _assert_no_sensitive(self, dump, label="metrics snapshot dump")

    def test_timer_records_only_fixed_outcomes(self) -> None:
        ticks = iter((10.0, 10.05, 11.0, 11.1))
        metrics = SafeMetrics()
        correlation = new_correlation_id()
        with metrics.timer(
            EventCode.ATTACHMENT_CALL,
            correlation,
            monotonic=lambda: next(ticks),
        ) as timer:
            timer.outcome(OutcomeCode.OVERLOADED)

        with self.assertRaises(TypeError):
            with metrics.timer(EventCode.JIRA_CALL, correlation) as timer:
                timer.outcome("not-an-outcome")  # type: ignore[arg-type]

        # Exception path collapses to fixed ERROR / CANCELLED codes.
        try:
            with metrics.timer(
                EventCode.GEMINI_CALL,
                correlation,
                monotonic=lambda: next(ticks),
            ):
                raise RuntimeError(
                    f"provider exception detail pat={TEST_ONLY_PAT} "
                    "https://jira.secret.example/browse/BOT-99"
                )
        except RuntimeError:
            pass

        snapshot = metrics.snapshot()
        self.assertEqual(
            snapshot.counters[(EventCode.ATTACHMENT_CALL, OutcomeCode.OVERLOADED)],
            1,
        )
        self.assertEqual(
            snapshot.counters[(EventCode.GEMINI_CALL, OutcomeCode.ERROR)],
            1,
        )
        for observation in snapshot.recent:
            _assert_no_sensitive(self, repr(observation), label="timer observation")


class WorkKeyAndCredentialReprPrivacy(unittest.TestCase):
    def test_work_key_repr_hides_actor_chat_and_draft_identifiers(self) -> None:
        actor_id = 1234567890
        chat_id = 9876543210
        draft_id = "draft-with-template-summary-SECRET"
        collection = WorkKey.for_collection(
            actor_id=actor_id,
            chat_id=chat_id,
            message_thread_id=42,
        )
        workflow = WorkKey.for_workflow(draft_id)

        for key in (collection, workflow):
            rendered = repr(key)
            self.assertIn("<opaque>", rendered)
            self.assertNotIn(str(actor_id), rendered)
            self.assertNotIn(str(chat_id), rendered)
            self.assertNotIn(draft_id, rendered)
            self.assertNotIn("template-summary", rendered)
            _assert_no_sensitive(self, rendered, label="WorkKey repr")

    def test_credentials_repr_redacts_pat(self) -> None:
        creds = JiraCredentials(
            jira_username="jira.user",
            jira_display_name="Jira User",
            jira_pat=TEST_ONLY_PAT,
        )
        rendered = repr(creds)
        self.assertIn("<redacted>", rendered)
        self.assertNotIn(TEST_ONLY_PAT, rendered)


class LogAndDenialSurfacePrivacy(unittest.TestCase):
    def test_application_error_log_excludes_update_and_secret_payloads(self) -> None:
        secret = (
            f"leaked {TEST_ONLY_PAT} chat_id=1234567890 "
            "file_id=AgADBAADSECRET_FILE_ID "
            "vpn.secret.example /etc/NetworkManager/system-connections/secret.nmconnection "
            "forwarded private message body"
        )
        context = SimpleNamespace(error=RuntimeError(secret))
        with patch("dztgbot.__main__.LOGGER") as logger:
            asyncio.run(handle_application_error({"text": secret, "callback": "j1:cfm:ab"}, context))  # type: ignore[arg-type]
            logger.error.assert_called_once()
            call_args = logger.error.call_args[0]
            rendered = call_args[0] % call_args[1:] if len(call_args) > 1 else call_args[0]
            self.assertIn("RuntimeError", rendered)
            _assert_no_sensitive(self, rendered, label="application error log")
            self.assertNotIn(secret, rendered)

    def test_callback_parse_and_denial_messages_are_fixed(self) -> None:
        evil = (
            "j1:cfm:"
            + "deadbeef" * 4
            + ":EXTRA_SECRET_PAYLOAD_https://jira.secret.example"
        )
        with self.assertRaises(CallbackParseError) as caught:
            parse_callback_data(evil)
        self.assertNotIn("EXTRA_SECRET", str(caught.exception))
        self.assertNotIn(evil, str(caught.exception))
        self.assertTrue(str(caught.exception).startswith("callback_"))
        _assert_no_sensitive(self, str(caught.exception), label="CallbackParseError")

        # Token hashing is deterministic and does not echo the raw token in errors.
        token = "a" * 32
        digest = hash_opaque_token(token)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, token)

        for code in DenialCode:
            message = user_message_for_denial(code)
            _assert_no_sensitive(self, message, label=f"denial {code.value}")
            self.assertNotIn("j1:", message)
            self.assertNotIn(TEST_ONLY_PAT, message)


if __name__ == "__main__":
    unittest.main()
