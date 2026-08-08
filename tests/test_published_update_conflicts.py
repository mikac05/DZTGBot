from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace

from dztgbot.domain.errors import ClassifiedOperationError, ErrorKind, Operation, RevisionConflictError, classify_unknown_mutation_outcome
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import JiraTaskTemplate, PublishedIssue
from dztgbot.infrastructure.persistence.workflow_sqlite import SQLiteWorkflowRepository
from dztgbot.services.submission_service import SubmissionService
from dztgbot.services.submission_service import canonical_template_document, complete_template_diff
from tests.test_submission_recovery import Gateway, draft, NOW


class PublishedUpdateConflictTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteWorkflowRepository(Path(self.temp.name) / "db.sqlite", enable_wal=False)
        await self.repo.initialize()
        self.service = SubmissionService(self.repo, Gateway())  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_complete_diff_and_revision_conflict(self) -> None:
        original = draft(DraftState.COMPLETE, revision=4)
        original = replace(original, published_issue=PublishedIssue("BOT-1", "1", "https://jira.invalid/browse/BOT-1", NOW))
        await self.repo.save(original)
        changed = JiraTaskTemplate("NEW", "Bug", "new", "new body", "High", ("x",), ("c",), "alice", ["accept"])
        with self.assertRaises(RevisionConflictError):
            await self.service.prepare_published_update("draft", changed, expected_revision=3)
        plan = await self.service.prepare_published_update("draft", changed, expected_revision=4)
        self.assertEqual(set(plan.changed_fields), set(complete_template_diff(original.template, changed)))  # type: ignore[arg-type]
        self.assertEqual(plan.draft.state, DraftState.UPDATE_REVIEW)

    async def test_concurrent_update_preparation_has_one_revision_winner(self) -> None:
        original = draft(DraftState.COMPLETE, revision=4)
        original = replace(
            original,
            published_issue=PublishedIssue(
                "BOT-1", "1", "https://jira.invalid/browse/BOT-1", NOW
            ),
        )
        await self.repo.save(original)
        assert original.template is not None
        first = replace(original.template, summary="first")
        second = replace(original.template, summary="second")

        outcomes = await asyncio.gather(
            self.service.prepare_published_update(
                "draft", first, expected_revision=original.revision
            ),
            self.service.prepare_published_update(
                "draft", second, expected_revision=original.revision
            ),
            return_exceptions=True,
        )
        winners = [
            outcome for outcome in outcomes if hasattr(outcome, "changed_fields")
        ]
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, RevisionConflictError)
        ]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        durable = await self.repo.get_by_id("draft")
        assert durable is not None and durable.template is not None
        self.assertEqual(durable.state, DraftState.UPDATE_REVIEW)
        self.assertEqual(durable.revision, original.revision + 1)
        self.assertIn(durable.template.summary, {"first", "second"})

    async def test_unknown_update_is_reconciled_by_complete_remote_fields(self) -> None:
        template = draft().template
        assert template is not None
        reviewing = replace(
            draft(DraftState.UPDATE_REVIEW, revision=5),
            published_issue=PublishedIssue("BOT-1", "1", "https://jira.invalid/browse/BOT-1", NOW),
        )
        await self.repo.save(reviewing)

        class UnknownUpdateGateway(Gateway):
            async def update_issue(self, issue_key, template, pat):
                raise ClassifiedOperationError(
                    classify_unknown_mutation_outcome(
                        operation=Operation.JIRA_UPDATE, kind=ErrorKind.TIMEOUT
                    )
                )

            async def get_issue(self, issue_key, pat):
                return SimpleNamespace(fields=canonical_template_document(template))

        service = SubmissionService(self.repo, UnknownUpdateGateway())  # type: ignore[arg-type]
        uncertain = await service.confirm_published_update("draft", "pat", expected_revision=5)
        self.assertEqual(uncertain.draft.state, DraftState.UPDATE_UNKNOWN)
        recovered = await service.reconcile_update("draft", "pat")
        self.assertEqual(recovered.draft.state, DraftState.COMPLETE)


if __name__ == "__main__":
    unittest.main()
