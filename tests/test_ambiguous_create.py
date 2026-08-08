from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from dztgbot.domain.errors import ClassifiedOperationError, ErrorKind, Operation, classify_unknown_mutation_outcome
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import PublishedIssue
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.services.submission_service import SubmissionService
from tests.test_submission_recovery import draft, NOW


class UnknownGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.matches = ()

    async def create_issue(self, template, pat, idempotency_key=None):
        self.calls += 1
        raise ClassifiedOperationError(classify_unknown_mutation_outcome(operation=Operation.JIRA_CREATE, kind=ErrorKind.TIMEOUT))

    async def find_by_request_hash(self, project_key, request_hash, pat):
        return self.matches


class AmbiguousCreateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteWorkflowRepository(Path(self.temp.name) / "db.sqlite", enable_wal=False)
        await self.repo.initialize()
        self.gateway = UnknownGateway()
        self.service = SubmissionService(self.repo, self.gateway)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_unknown_never_blind_retries_and_absence_is_inconclusive(self) -> None:
        await self.repo.save(draft())
        result = await self.service.submit("draft", "pat")
        self.assertEqual(result.draft.state, DraftState.SUBMISSION_UNKNOWN)
        with self.assertRaises(Exception):
            await self.service.submit("draft", "pat")
        self.assertEqual(self.gateway.calls, 1)
        reconciled = await self.service.reconcile_create("draft", "pat")
        self.assertTrue(reconciled.reconciliation_pending)
        self.assertEqual(reconciled.draft.state, DraftState.SUBMISSION_UNKNOWN)

    async def test_positive_reconciliation_finishes_without_second_create(self) -> None:
        await self.repo.save(draft())
        await self.service.submit("draft", "pat")
        self.gateway.matches = (PublishedIssue("BOT-9", "9", "https://jira.invalid/browse/BOT-9", NOW),)
        result = await self.service.reconcile_create("draft", "pat")
        self.assertEqual(result.draft.state, DraftState.CREATED)
        self.assertEqual(self.gateway.calls, 1)


if __name__ == "__main__":
    unittest.main()
