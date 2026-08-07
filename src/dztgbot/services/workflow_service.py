"""Pure application service for managing Draft aggregate roots and state transitions."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Sequence

from dztgbot.domain.errors import (
    DomainError,
    ErrorClassification,
    ErrorKind,
    InvalidStateTransitionError,
    MutationCertainty,
    Operation,
    Retryability,
    RevisionConflictError,
    SafeErrorCode,
    StateConflictError,
)
from dztgbot.domain.fsm import DraftState, is_transition_allowed, validate_transition
from dztgbot.domain.models import Draft, JiraTaskTemplate
from dztgbot.domain.ports import ClockPort, DraftRepositoryPort, IdGeneratorPort

LOGGER = logging.getLogger(__name__)


class DraftNotFoundError(DomainError):
    """Raised when a requested draft_id does not exist."""

    def __init__(self, draft_id: str) -> None:
        self.draft_id = draft_id
        super().__init__(
            ErrorClassification(
                kind=ErrorKind.STALE_ACTION,
                operation=Operation.WORKFLOW_TRANSITION,
                retryability=Retryability.NEVER,
                mutation_certainty=MutationCertainty.NOT_APPLICABLE,
                safe_code=SafeErrorCode.STALE_ACTION,
            )
        )


class DraftAccessDeniedError(DomainError):
    """Raised when a draft action is attempted by an unauthorized actor or chat."""

    def __init__(self, draft_id: str, actor_id: int, chat_id: int) -> None:
        self.draft_id = draft_id
        self.actor_id = actor_id
        self.chat_id = chat_id
        super().__init__(
            ErrorClassification(
                kind=ErrorKind.PERMISSION,
                operation=Operation.WORKFLOW_TRANSITION,
                retryability=Retryability.NEVER,
                mutation_certainty=MutationCertainty.NOT_APPLICABLE,
                safe_code=SafeErrorCode.PERMISSION_DENIED,
            )
        )


class WorkflowService:
    """Pure use-case orchestrator for Draft creation, template edits, and cancellations."""

    def __init__(
        self,
        repository: DraftRepositoryPort,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._id_generator = id_generator

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    def _generate_id(self) -> str:
        if self._id_generator is not None:
            return self._id_generator.generate_uuid()
        import uuid
        return str(uuid.uuid4())

    async def get_draft(
        self,
        draft_id: str,
        actor_id: int | None = None,
        chat_id: int | None = None,
    ) -> Draft:
        """Retrieve a draft by ID and verify actor/chat permissions if provided."""
        draft = await self._repository.get_by_id(draft_id)
        if draft is None:
            raise DraftNotFoundError(draft_id)

        if actor_id is not None and draft.owner_id != actor_id:
            raise DraftAccessDeniedError(draft_id, actor_id, chat_id or 0)
        if chat_id is not None and draft.chat_id != chat_id:
            raise DraftAccessDeniedError(draft_id, actor_id or 0, chat_id)

        return draft

    async def create_manual_draft(
        self,
        owner_id: int,
        chat_id: int,
        template: JiraTaskTemplate,
        message_thread_id: int | None = None,
        draft_id: str | None = None,
    ) -> Draft:
        """Create a new manual draft in REVIEW state with the specified template."""
        now = self._now()
        id_str = draft_id or self._generate_id()
        draft = Draft(
            draft_id=id_str,
            owner_id=owner_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            state=DraftState.REVIEW,
            revision=1,
            template=template,
            created_at=now,
            updated_at=now,
        )
        await self._repository.save(draft)
        LOGGER.info("Created manual draft (%s) for owner %s", id_str, owner_id)
        return draft

    async def update_template(
        self,
        draft_id: str,
        owner_id: int,
        chat_id: int,
        new_template: JiraTaskTemplate,
        expected_revision: int,
    ) -> Draft:
        """Apply a new template to an existing draft and advance its state/revision."""
        draft = await self.get_draft(draft_id, actor_id=owner_id, chat_id=chat_id)

        if draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)

        # Ensure transition from current state to REVIEW or EDITING is allowed
        if draft.state not in (DraftState.REVIEW, DraftState.EDITING, DraftState.SUBMISSION_RETRYABLE):
            validate_transition(draft.state, DraftState.REVIEW)

        now = self._now()
        updated_draft = Draft(
            draft_id=draft.draft_id,
            owner_id=draft.owner_id,
            chat_id=draft.chat_id,
            message_thread_id=draft.message_thread_id,
            state=DraftState.REVIEW,
            revision=draft.revision + 1,
            template=new_template,
            source_messages=draft.source_messages,
            attachments=draft.attachments,
            created_at=draft.created_at,
            updated_at=now,
            published_issue=draft.published_issue,
            last_error=None,
        )
        await self._repository.save(updated_draft)
        return updated_draft

    async def toggle_issue_type(
        self,
        draft_id: str,
        owner_id: int,
        chat_id: int,
        expected_revision: int,
        available_types: Sequence[str] = ("Task", "Bug", "Story"),
    ) -> Draft:
        """Cycle the issue type of a draft's template to the next available type."""
        draft = await self.get_draft(draft_id, actor_id=owner_id, chat_id=chat_id)
        if draft.template is None:
            raise StateConflictError(DraftState.REVIEW, draft.state)

        if draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)

        current_type = draft.template.issue_type
        if current_type in available_types:
            idx = available_types.index(current_type)
            next_type = available_types[(idx + 1) % len(available_types)]
        else:
            next_type = available_types[0] if available_types else "Task"

        new_template = JiraTaskTemplate(
            project_key=draft.template.project_key,
            issue_type=next_type,
            summary=draft.template.summary,
            description=draft.template.description,
            priority=draft.template.priority,
            labels=draft.template.labels,
            components=draft.template.components,
            assignee=draft.template.assignee,
            acceptance_criteria=draft.template.acceptance_criteria,
        )
        return await self.update_template(
            draft_id=draft_id,
            owner_id=owner_id,
            chat_id=chat_id,
            new_template=new_template,
            expected_revision=expected_revision,
        )

    async def toggle_priority(
        self,
        draft_id: str,
        owner_id: int,
        chat_id: int,
        expected_revision: int,
        available_priorities: Sequence[str] = ("Lowest", "Low", "Medium", "High", "Highest"),
    ) -> Draft:
        """Cycle the priority of a draft's template to the next available priority."""
        draft = await self.get_draft(draft_id, actor_id=owner_id, chat_id=chat_id)
        if draft.template is None:
            raise StateConflictError(DraftState.REVIEW, draft.state)

        if draft.revision != expected_revision:
            raise RevisionConflictError(expected_revision, draft.revision)

        current_prio = draft.template.priority
        if current_prio in available_priorities:
            idx = available_priorities.index(current_prio)
            next_prio = available_priorities[(idx + 1) % len(available_priorities)]
        else:
            next_prio = available_priorities[0] if available_priorities else "Medium"

        new_template = JiraTaskTemplate(
            project_key=draft.template.project_key,
            issue_type=draft.template.issue_type,
            summary=draft.template.summary,
            description=draft.template.description,
            priority=next_prio,
            labels=draft.template.labels,
            components=draft.template.components,
            assignee=draft.template.assignee,
            acceptance_criteria=draft.template.acceptance_criteria,
        )
        return await self.update_template(
            draft_id=draft_id,
            owner_id=owner_id,
            chat_id=chat_id,
            new_template=new_template,
            expected_revision=expected_revision,
        )

    async def cancel_draft(
        self,
        draft_id: str,
        owner_id: int,
        chat_id: int,
        expected_revision: int,
    ) -> Draft:
        """Cancel an active draft, moving it to CANCELLED state via CAS."""
        draft = await self.get_draft(draft_id, actor_id=owner_id, chat_id=chat_id)
        validate_transition(draft.state, DraftState.CANCELLED)

        return await self._repository.compare_and_swap_state(
            draft_id=draft_id,
            expected_revision=expected_revision,
            target_state=DraftState.CANCELLED,
            last_error=None,
        )

    async def expire_eligible_drafts(self, before_utc: datetime) -> int:
        """Find and expire all non-terminal drafts created before specified cutoff."""
        expired_drafts = await self._repository.list_expired(before_utc)
        count = 0
        for draft in expired_drafts:
            try:
                await self._repository.compare_and_swap_state(
                    draft_id=draft.draft_id,
                    expected_revision=draft.revision,
                    target_state=DraftState.EXPIRED,
                    last_error="Draft expired due to inactivity",
                )
                count += 1
            except (RevisionConflictError, StateConflictError, InvalidStateTransitionError):
                continue
        return count
