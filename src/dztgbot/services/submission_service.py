"""Failure-preserving Jira create/update orchestration.

Every external mutation is preceded by a durable one-winner attempt claim.
Unknown outcomes are reconciled; the service never re-dispatches them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping, Protocol, Sequence
import uuid

from dztgbot.domain.errors import (
    DomainError,
    ErrorClassification,
    ErrorKind,
    MutationCertainty,
    Operation,
    Retryability,
    RevisionConflictError,
    SafeErrorCode,
)
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft, JiraTaskTemplate, PublishedIssue, SubmissionAttempt


class SubmissionRepository(Protocol):
    async def get_by_id(self, draft_id: str) -> Draft | None: ...
    async def compare_and_swap_state(self, draft_id: str, expected_revision: int, target_state: DraftState, last_error: str | None = None) -> Draft: ...
    async def save(self, draft: Draft) -> None: ...
    async def claim_attempt(self, attempt: SubmissionAttempt) -> bool: ...
    async def update_attempt(self, attempt: SubmissionAttempt) -> None: ...
    async def get_latest_attempt(self, draft_id: str) -> SubmissionAttempt | None: ...
    async def store_published_issue(self, draft_id: str, issue: PublishedIssue) -> None: ...


class SubmissionGateway(Protocol):
    async def create_issue(self, template: JiraTaskTemplate, pat: str, idempotency_key: str | None = None) -> PublishedIssue: ...
    async def update_issue(self, issue_key: str, template: JiraTaskTemplate, pat: str) -> None: ...
    async def find_by_request_hash(self, project_key: str, request_hash: str, pat: str) -> Sequence[PublishedIssue]: ...
    async def get_issue(self, issue_key: str, pat: str) -> object: ...


class SubmissionServiceError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    draft: Draft
    attempt: SubmissionAttempt
    published_issue: PublishedIssue | None
    reconciliation_pending: bool = False


@dataclass(frozen=True, slots=True)
class PublishedUpdatePlan:
    draft: Draft
    changed_fields: Mapping[str, object]

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_fields)


def canonical_template_document(template: JiraTaskTemplate) -> dict[str, object]:
    description = template.description
    if template.acceptance_criteria:
        description += "\n\nAcceptance Criteria:\n" + "\n".join(
            f"* {criterion}" for criterion in template.acceptance_criteria
        )
    fields: dict[str, object] = {
        "project": {"key": template.project_key},
        "issuetype": {"name": template.issue_type},
        "summary": template.summary,
        "description": description,
        "priority": {"name": template.priority},
        "labels": list(template.labels),
        "components": [{"name": value} for value in template.components],
    }
    if template.assignee:
        fields["assignee"] = {"name": template.assignee}
    return fields


def canonical_request_hash(template: JiraTaskTemplate) -> str:
    payload = json.dumps(
        canonical_template_document(template),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def complete_template_diff(before: JiraTaskTemplate, after: JiraTaskTemplate) -> dict[str, object]:
    previous = canonical_template_document(before)
    current = canonical_template_document(after)
    return {key: value for key, value in current.items() if previous.get(key) != value}


class SubmissionService:
    def __init__(self, repository: SubmissionRepository, gateway: SubmissionGateway) -> None:
        self._repository = repository
        self._gateway = gateway

    async def submit(
        self, draft_id: str, pat: str, *, expected_revision: int | None = None
    ) -> SubmissionResult:
        draft = await self._required_draft(draft_id)
        if expected_revision is not None and draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)
        if draft.state not in {DraftState.REVIEW, DraftState.SUBMISSION_RETRYABLE} or draft.template is None:
            raise self._state_error()
        submitting = await self._repository.compare_and_swap_state(
            draft_id, draft.revision, DraftState.SUBMITTING
        )
        attempt = await self._new_attempt(submitting, draft.template)
        if not await self._repository.claim_attempt(attempt):
            raise self._conflict_error()
        try:
            issue = await self._gateway.create_issue(
                draft.template, pat, idempotency_key=attempt.request_hash
            )
        except DomainError as error:
            return await self._finish_failure(submitting, attempt, error)
        except Exception as error:
            return await self._finish_unknown(submitting, attempt, error)

        completed_attempt = replace(
            attempt, status="success", completed_at=self._now()
        )
        await self._repository.update_attempt(completed_attempt)
        await self._repository.store_published_issue(draft_id, issue)
        created = await self._repository.compare_and_swap_state(
            draft_id, submitting.revision, DraftState.CREATED
        )
        return SubmissionResult(created, completed_attempt, issue)

    async def recover_stalled(self, draft_id: str) -> SubmissionResult:
        """Recover local crash windows without dispatching any provider request."""

        draft = await self._required_draft(draft_id)
        attempt = await self._repository.get_latest_attempt(draft_id)
        if draft.state is not DraftState.SUBMITTING or attempt is None:
            raise self._state_error()
        if draft.published_issue is not None:
            success = attempt if attempt.status == "success" else replace(
                attempt, status="success", completed_at=self._now(), error_summary=None
            )
            if attempt.status != "success":
                await self._repository.update_attempt(success)
            created = await self._repository.compare_and_swap_state(
                draft_id, draft.revision, DraftState.CREATED
            )
            return SubmissionResult(created, success, draft.published_issue)
        if attempt.status == "pending":
            unknown = replace(attempt, status="unknown", completed_at=self._now(), error_summary="outcome_unknown")
            await self._repository.update_attempt(unknown)
            changed = await self._repository.compare_and_swap_state(
                draft_id, draft.revision, DraftState.SUBMISSION_UNKNOWN, "outcome_unknown"
            )
            return SubmissionResult(changed, unknown, None, True)
        raise self._state_error()

    async def reconcile_create(self, draft_id: str, pat: str) -> SubmissionResult:
        draft = await self._required_draft(draft_id)
        attempt = await self._repository.get_latest_attempt(draft_id)
        if draft.state is not DraftState.SUBMISSION_UNKNOWN or attempt is None or attempt.status != "unknown" or draft.template is None:
            raise self._state_error()
        matches = tuple(await self._gateway.find_by_request_hash(
            draft.template.project_key, attempt.request_hash, pat
        ))
        if len(matches) != 1:
            # Zero is inconclusive and multiple matches require human intervention.
            return SubmissionResult(draft, attempt, None, True)
        issue = matches[0]
        success = replace(attempt, status="success", completed_at=self._now(), error_summary=None)
        await self._repository.update_attempt(success)
        await self._repository.store_published_issue(draft_id, issue)
        created = await self._repository.compare_and_swap_state(
            draft_id, draft.revision, DraftState.CREATED
        )
        return SubmissionResult(created, success, issue)

    async def allow_retry_after_negative_reconciliation(self, draft_id: str) -> Draft:
        """Record a human-reviewed bounded reconciliation result as not created."""

        draft = await self._required_draft(draft_id)
        attempt = await self._repository.get_latest_attempt(draft_id)
        if draft.state is not DraftState.SUBMISSION_UNKNOWN or attempt is None or attempt.status != "unknown":
            raise self._state_error()
        failed = replace(attempt, status="failed", completed_at=self._now(), error_summary="reconciled_not_created")
        await self._repository.update_attempt(failed)
        return await self._repository.compare_and_swap_state(
            draft_id, draft.revision, DraftState.SUBMISSION_RETRYABLE, "reconciled_not_created"
        )

    async def prepare_published_update(
        self,
        draft_id: str,
        new_template: JiraTaskTemplate,
        *,
        expected_revision: int,
    ) -> PublishedUpdatePlan:
        draft = await self._required_draft(draft_id)
        if draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)
        if draft.state not in {DraftState.COMPLETE, DraftState.ATTACHMENT_PARTIAL} or draft.template is None or draft.published_issue is None:
            raise self._state_error()
        changed_fields = complete_template_diff(draft.template, new_template)
        updated = replace(
            draft,
            state=DraftState.UPDATE_REVIEW,
            revision=draft.revision + 1,
            template=new_template,
            updated_at=self._now(),
        )
        await self._repository.save(updated)
        return PublishedUpdatePlan(updated, changed_fields)

    async def confirm_published_update(
        self, draft_id: str, pat: str, *, expected_revision: int
    ) -> SubmissionResult:
        draft = await self._required_draft(draft_id)
        if draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)
        if draft.state is not DraftState.UPDATE_REVIEW or draft.template is None or draft.published_issue is None:
            raise self._state_error()
        updating = await self._repository.compare_and_swap_state(
            draft_id, draft.revision, DraftState.UPDATING
        )
        attempt = await self._new_attempt(updating, draft.template)
        if not await self._repository.claim_attempt(attempt):
            raise self._conflict_error()
        try:
            await self._gateway.update_issue(
                draft.published_issue.issue_key, draft.template, pat
            )
        except DomainError as error:
            return await self._finish_update_failure(updating, attempt, error)
        except Exception as error:
            return await self._finish_update_unknown(updating, attempt, error)
        success = replace(attempt, status="success", completed_at=self._now())
        await self._repository.update_attempt(success)
        complete = await self._repository.compare_and_swap_state(
            draft_id, updating.revision, DraftState.COMPLETE
        )
        return SubmissionResult(complete, success, draft.published_issue)

    async def reconcile_update(self, draft_id: str, pat: str) -> SubmissionResult:
        draft = await self._required_draft(draft_id)
        attempt = await self._repository.get_latest_attempt(draft_id)
        if draft.state is not DraftState.UPDATE_UNKNOWN or attempt is None or attempt.status != "unknown" or draft.template is None or draft.published_issue is None:
            raise self._state_error()
        remote = await self._gateway.get_issue(draft.published_issue.issue_key, pat)
        fields = getattr(remote, "fields", None)
        expected = canonical_template_document(draft.template)
        if not isinstance(fields, Mapping) or any(fields.get(key) != value for key, value in expected.items()):
            return SubmissionResult(draft, attempt, draft.published_issue, True)
        success = replace(attempt, status="success", completed_at=self._now(), error_summary=None)
        await self._repository.update_attempt(success)
        complete = await self._repository.compare_and_swap_state(
            draft_id, draft.revision, DraftState.COMPLETE
        )
        return SubmissionResult(complete, success, draft.published_issue)

    async def _new_attempt(self, draft: Draft, template: JiraTaskTemplate) -> SubmissionAttempt:
        previous = await self._repository.get_latest_attempt(draft.draft_id)
        return SubmissionAttempt(
            attempt_id=str(uuid.uuid4()),
            draft_id=draft.draft_id,
            request_hash=canonical_request_hash(template),
            attempt_number=1 if previous is None else previous.attempt_number + 1,
            started_at=self._now(),
        )

    async def _finish_failure(self, draft: Draft, attempt: SubmissionAttempt, error: DomainError) -> SubmissionResult:
        if error.classification.mutation_certainty is MutationCertainty.UNKNOWN:
            return await self._finish_unknown(draft, attempt, error)
        failed = replace(attempt, status="failed", completed_at=self._now(), error_summary=error.classification.safe_code.value)
        await self._repository.update_attempt(failed)
        retryable = await self._repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.SUBMISSION_RETRYABLE, error.classification.safe_code.value
        )
        return SubmissionResult(retryable, failed, None)

    async def _finish_unknown(self, draft: Draft, attempt: SubmissionAttempt, error: BaseException) -> SubmissionResult:
        unknown = replace(attempt, status="unknown", completed_at=self._now(), error_summary="outcome_unknown")
        await self._repository.update_attempt(unknown)
        changed = await self._repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.SUBMISSION_UNKNOWN, "outcome_unknown"
        )
        return SubmissionResult(changed, unknown, None, True)

    async def _finish_update_failure(self, draft: Draft, attempt: SubmissionAttempt, error: DomainError) -> SubmissionResult:
        if error.classification.mutation_certainty is MutationCertainty.UNKNOWN:
            return await self._finish_update_unknown(draft, attempt, error)
        failed = replace(attempt, status="failed", completed_at=self._now(), error_summary=error.classification.safe_code.value)
        await self._repository.update_attempt(failed)
        changed = await self._repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.UPDATE_RETRYABLE, error.classification.safe_code.value
        )
        return SubmissionResult(changed, failed, draft.published_issue)

    async def _finish_update_unknown(self, draft: Draft, attempt: SubmissionAttempt, error: BaseException) -> SubmissionResult:
        unknown = replace(attempt, status="unknown", completed_at=self._now(), error_summary="outcome_unknown")
        await self._repository.update_attempt(unknown)
        changed = await self._repository.compare_and_swap_state(
            draft.draft_id, draft.revision, DraftState.UPDATE_UNKNOWN, "outcome_unknown"
        )
        return SubmissionResult(changed, unknown, draft.published_issue, True)

    async def _required_draft(self, draft_id: str) -> Draft:
        draft = await self._repository.get_by_id(draft_id)
        if draft is None:
            raise self._state_error()
        return draft

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _state_error() -> SubmissionServiceError:
        return SubmissionServiceError(ErrorClassification(ErrorKind.CONFLICT, Operation.WORKFLOW_TRANSITION, Retryability.NEVER, MutationCertainty.NOT_APPLICABLE, SafeErrorCode.STATE_CONFLICT))

    @staticmethod
    def _conflict_error() -> SubmissionServiceError:
        return SubmissionServiceError(ErrorClassification(ErrorKind.CONFLICT, Operation.PERSISTENCE, Retryability.NEVER, MutationCertainty.NOT_APPLICABLE, SafeErrorCode.STATE_CONFLICT))


__all__ = [
    "PublishedUpdatePlan", "SubmissionResult", "SubmissionService",
    "SubmissionServiceError", "canonical_request_hash",
    "canonical_template_document", "complete_template_diff",
]
