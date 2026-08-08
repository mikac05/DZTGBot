from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate, PublishedIssue
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.services.submission_service import SubmissionService


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def draft(state: DraftState = DraftState.REVIEW, revision: int = 1) -> Draft:
    return Draft("draft", 1, 1, state=state, revision=revision, template=JiraTaskTemplate("BOT", "Task", "summary", "description", "Medium"), created_at=NOW, updated_at=NOW)


class Gateway:
    def __init__(self) -> None:
        self.creates = 0
        self.matches: tuple[PublishedIssue, ...] = ()

    async def create_issue(self, template, pat, idempotency_key=None):
        self.creates += 1
        return PublishedIssue("BOT-1", "1", "https://jira.invalid/browse/BOT-1", NOW)

    async def find_by_request_hash(self, project_key, request_hash, pat):
        return self.matches


class SubmissionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteWorkflowRepository(Path(self.temp.name) / "db.sqlite", enable_wal=False)
        await self.repo.initialize()
        self.gateway = Gateway()
        self.service = SubmissionService(self.repo, self.gateway)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_attempt_is_durable_and_success_transitions_created(self) -> None:
        await self.repo.save(draft())
        result = await self.service.submit("draft", "pat", expected_revision=1)
        self.assertEqual(result.draft.state, DraftState.CREATED)
        self.assertEqual(result.attempt.status, "success")
        self.assertEqual(len(result.attempt.request_hash), 64)
        self.assertEqual((await self.repo.get_latest_attempt("draft")).status, "success")  # type: ignore[union-attr]

    async def test_stalled_pending_attempt_becomes_unknown_without_network(self) -> None:
        await self.repo.save(draft())
        submitting = await self.repo.compare_and_swap_state("draft", 1, DraftState.SUBMITTING)
        from dztgbot.domain.models import SubmissionAttempt
        attempt = SubmissionAttempt("attempt", "draft", "a" * 64, 1, NOW)
        self.assertTrue(await self.repo.claim_attempt(attempt))
        recovered = await self.service.recover_stalled("draft")
        self.assertEqual(recovered.draft.state, DraftState.SUBMISSION_UNKNOWN)
        self.assertEqual(self.gateway.creates, 0)


if __name__ == "__main__":
    unittest.main()
