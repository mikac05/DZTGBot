"""Unit tests for WorkflowService use-case orchestrator."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from dztgbot.domain.errors import (
    InvalidStateTransitionError,
    RevisionConflictError,
)
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate
from dztgbot.services.workflow_service import (
    DraftAccessDeniedError,
    DraftNotFoundError,
    WorkflowService,
)


class FakeClock:
    def __init__(self, start_time: datetime | None = None) -> None:
        self._current = start_time or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


class FakeIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def generate_uuid(self) -> str:
        self._counter += 1
        return f"test-uuid-{self._counter}"

    def generate_opaque_token(self, length_bytes: int = 16) -> str:
        return "0123456789abcdef" * (length_bytes // 16)


class WorkflowServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.id_gen = FakeIdGenerator()
        self.repository = MagicMock()
        self.repository.save = AsyncMock()
        self.repository.get_by_id = AsyncMock(return_value=None)
        self.repository.compare_and_swap_state = AsyncMock()
        self.repository.list_expired = AsyncMock(return_value=[])

        self.service = WorkflowService(
            repository=self.repository,
            clock=self.clock,
            id_generator=self.id_gen,
        )
        self.template = JiraTaskTemplate(
            project_key="NGSSA3",
            issue_type="Task",
            summary="Original Summary",
            description="Original Description",
            priority="Medium",
            acceptance_criteria=["Crit 1"],
        )

    async def test_create_manual_draft_success(self) -> None:
        draft = await self.service.create_manual_draft(
            owner_id=100,
            chat_id=-200,
            template=self.template,
        )
        self.assertEqual(draft.draft_id, "test-uuid-1")
        self.assertEqual(draft.owner_id, 100)
        self.assertEqual(draft.chat_id, -200)
        self.assertEqual(draft.state, DraftState.REVIEW)
        self.assertEqual(draft.revision, 1)
        self.assertEqual(draft.template.summary, "Original Summary")
        self.repository.save.assert_called_once_with(draft)

    async def test_get_draft_not_found(self) -> None:
        with self.assertRaises(DraftNotFoundError):
            await self.service.get_draft("non-existent-id")

    async def test_get_draft_access_denied(self) -> None:
        existing_draft = Draft.create_new(owner_id=100, chat_id=-200, draft_id="dft-100")
        self.repository.get_by_id.return_value = existing_draft

        # Wrong owner
        with self.assertRaises(DraftAccessDeniedError):
            await self.service.get_draft("dft-100", actor_id=999, chat_id=-200)

        # Wrong chat
        with self.assertRaises(DraftAccessDeniedError):
            await self.service.get_draft("dft-100", actor_id=100, chat_id=-999)

    async def test_update_template_success(self) -> None:
        existing = Draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            state=DraftState.REVIEW,
            revision=1,
            template=self.template,
        )
        self.repository.get_by_id.return_value = existing

        new_template = JiraTaskTemplate(
            project_key="NGSSA3",
            issue_type="Bug",
            summary="Updated Summary",
            description="Updated Description",
            priority="High",
        )
        updated = await self.service.update_template(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            new_template=new_template,
            expected_revision=1,
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.state, DraftState.REVIEW)
        self.assertEqual(updated.template.issue_type, "Bug")
        self.assertEqual(updated.template.summary, "Updated Summary")
        self.repository.save.assert_called_once_with(updated)

    async def test_update_template_revision_mismatch(self) -> None:
        existing = Draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            state=DraftState.REVIEW,
            revision=2,
            template=self.template,
        )
        self.repository.get_by_id.return_value = existing

        with self.assertRaises(RevisionConflictError):
            await self.service.update_template(
                draft_id="dft-1",
                owner_id=100,
                chat_id=-200,
                new_template=self.template,
                expected_revision=1,  # Stale revision
            )

    async def test_toggle_issue_type_cycles(self) -> None:
        existing = Draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            state=DraftState.REVIEW,
            revision=1,
            template=self.template,  # "Task"
        )
        self.repository.get_by_id.return_value = existing

        # Toggle from Task -> Bug
        updated = await self.service.toggle_issue_type(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            expected_revision=1,
        )
        self.assertEqual(updated.template.issue_type, "Bug")
        self.assertEqual(updated.revision, 2)

    async def test_toggle_priority_cycles(self) -> None:
        existing = Draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            state=DraftState.REVIEW,
            revision=1,
            template=self.template,  # "Medium"
        )
        self.repository.get_by_id.return_value = existing

        # Toggle Medium -> High
        updated = await self.service.toggle_priority(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            expected_revision=1,
        )
        self.assertEqual(updated.template.priority, "High")

    async def test_cancel_draft_success(self) -> None:
        existing = Draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            state=DraftState.REVIEW,
            revision=1,
            template=self.template,
        )
        self.repository.get_by_id.return_value = existing

        cancelled_draft = Draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            state=DraftState.CANCELLED,
            revision=2,
            template=self.template,
        )
        self.repository.compare_and_swap_state.return_value = cancelled_draft

        res = await self.service.cancel_draft(
            draft_id="dft-1",
            owner_id=100,
            chat_id=-200,
            expected_revision=1,
        )
        self.assertEqual(res.state, DraftState.CANCELLED)
        self.repository.compare_and_swap_state.assert_called_once_with(
            draft_id="dft-1",
            expected_revision=1,
            target_state=DraftState.CANCELLED,
            last_error=None,
        )

    async def test_expire_eligible_drafts(self) -> None:
        d1 = Draft(draft_id="d1", owner_id=100, chat_id=-200, state=DraftState.REVIEW, revision=1)
        d2 = Draft(draft_id="d2", owner_id=101, chat_id=-201, state=DraftState.COLLECTING, revision=1)
        self.repository.list_expired.return_value = [d1, d2]

        count = await self.service.expire_eligible_drafts(before_utc=datetime.now(timezone.utc))
        self.assertEqual(count, 2)
        self.assertEqual(self.repository.compare_and_swap_state.call_count, 2)


if __name__ == "__main__":
    unittest.main()
