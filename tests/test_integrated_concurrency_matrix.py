"""Deterministic Phase 6 cross-workflow and callback concurrency matrix."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.callbacks import CallbackAction
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate, SourceMessageRef
from dztgbot.domain.policy import DenialCode
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.services.callback_service import CallbackService
from dztgbot.services.intake_service import IntakeService


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class SequenceIds:
    def __init__(self) -> None:
        self.value = 0

    def generate_uuid(self) -> str:
        self.value += 1
        return f"integrated-{self.value}"

    def generate_opaque_token(self, length_bytes: int = 16) -> str:
        return f"{self.value:032x}"


class NoopScheduler:
    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[], Awaitable[object]]] = {}

    def schedule_timer(
        self,
        job_id: str,
        delay_seconds: float,
        callback: Callable[[], Awaitable[object]],
    ) -> None:
        self.callbacks[job_id] = callback

    def cancel_timer(self, job_id: str) -> bool:
        return self.callbacks.pop(job_id, None) is not None


class Rules:
    async def get_rules(self) -> str:
        return "approved"


class OrderedAnalyzer:
    """Lets the test complete independent analyses in the opposite order."""

    def __init__(self) -> None:
        self.started: dict[int, asyncio.Event] = {}
        self.release: dict[int, asyncio.Event] = {}

    async def analyze_messages(
        self,
        messages: Sequence[SourceMessageRef],
        rules_text: str,
        default_project_key: str,
    ) -> JiraTaskTemplate:
        message_id = messages[0].message_id
        self.started.setdefault(message_id, asyncio.Event()).set()
        await self.release.setdefault(message_id, asyncio.Event()).wait()
        return JiraTaskTemplate(
            default_project_key,
            "Task",
            f"message-{message_id}",
            f"chat-{messages[0].chat_id}",
            "Medium",
        )


def source(message_id: int, chat_id: int, sender_id: int) -> SourceMessageRef:
    return SourceMessageRef(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=f"body-{message_id}",
        received_at=NOW,
    )


def review_draft(draft_id: str = "callback-draft") -> Draft:
    return Draft(
        draft_id=draft_id,
        owner_id=42,
        chat_id=42,
        state=DraftState.REVIEW,
        revision=1,
        template=JiraTaskTemplate("BOT", "Task", "summary", "description", "Medium"),
        created_at=NOW,
        updated_at=NOW,
    )


class IntegratedConcurrencyMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(
            Path(self.temporary_directory.name) / "workflow.sqlite3",
            enable_wal=False,
        )
        await self.repository.initialize()

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.temporary_directory.cleanup()

    async def test_cross_chat_analysis_completes_out_of_order_without_cross_talk(self) -> None:
        analyzer = OrderedAnalyzer()
        service = IntakeService(
            repository=self.repository,
            analyzer=analyzer,
            rules_repository=Rules(),
            scheduler=NoopScheduler(),
            clock=FixedClock(),
            id_generator=SequenceIds(),
            default_project_key="BOT",
        )
        first = await service.collect_message(
            owner_id=11,
            chat_id=101,
            message_thread_id=None,
            message=source(1, 101, 11),
        )
        second = await service.collect_message(
            owner_id=22,
            chat_id=202,
            message_thread_id=None,
            message=source(2, 202, 22),
        )

        first_task = asyncio.create_task(
            service.flush_scope(owner_id=11, chat_id=101, message_thread_id=None)
        )
        second_task = asyncio.create_task(
            service.flush_scope(owner_id=22, chat_id=202, message_thread_id=None)
        )
        while 1 not in analyzer.started or 2 not in analyzer.started:
            await asyncio.sleep(0)

        analyzer.release[2].set()
        second_result = await second_task
        self.assertFalse(first_task.done())
        analyzer.release[1].set()
        first_result = await first_task

        assert first_result is not None and second_result is not None
        self.assertEqual(first_result.draft_id, first.draft_id)
        self.assertEqual(first_result.chat_id, 101)
        self.assertEqual(first_result.owner_id, 11)
        self.assertEqual(first_result.template.summary, "message-1")  # type: ignore[union-attr]
        self.assertEqual(second_result.draft_id, second.draft_id)
        self.assertEqual(second_result.chat_id, 202)
        self.assertEqual(second_result.owner_id, 22)
        self.assertEqual(second_result.template.summary, "message-2")  # type: ignore[union-attr]

    async def test_concurrent_double_click_has_one_winner(self) -> None:
        draft = review_draft()
        await self.repository.save(draft)
        callbacks = CallbackService(self.repository, self.repository, FixedClock())
        issued = await callbacks.issue_preview_buttons(
            draft,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=700,
        )
        raw = issued[CallbackAction.CONFIRM].callback_data

        async def authorize() -> object:
            return await callbacks.authorize(
                raw_callback_data=raw,
                actor_user_id=42,
                chat_id=42,
                chat_type="private",
                preview_message_id=700,
            )

        results = await asyncio.gather(authorize(), authorize())
        self.assertEqual(sum(bool(result.allowed) for result in results), 1)  # type: ignore[attr-defined]
        loser = next(result for result in results if not result.allowed)  # type: ignore[attr-defined]
        self.assertEqual(loser.denial_code, DenialCode.TOKEN_CONSUMED)  # type: ignore[attr-defined]

    async def test_revision_change_makes_exact_old_callback_stale(self) -> None:
        draft = review_draft("stale-draft")
        await self.repository.save(draft)
        callbacks = CallbackService(self.repository, self.repository, FixedClock())
        issued = await callbacks.issue_preview_buttons(
            draft,
            actions=(CallbackAction.EDIT,),
            preview_message_id=701,
        )
        await self.repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.EDITING
        )

        result = await callbacks.authorize(
            raw_callback_data=issued[CallbackAction.EDIT].callback_data,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=701,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.denial_code, DenialCode.STALE_REVISION)


if __name__ == "__main__":
    unittest.main()
