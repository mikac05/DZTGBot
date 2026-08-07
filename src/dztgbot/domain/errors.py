"""Typed, privacy-safe domain error classifications.

Retryability and external-mutation certainty are deliberately independent.  In
particular, an operation whose outcome is unknown is never eligible for retry
until a separate reconciliation step establishes what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    """Stable machine-readable failure categories."""

    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    CONNECTIVITY = "connectivity"
    TIMEOUT = "timeout"
    PROVIDER_REJECTION = "provider_rejection"
    PROVIDER_CONTRACT = "provider_contract"
    CONFLICT = "conflict"
    STALE_ACTION = "stale_action"
    STORAGE = "storage"
    INTERNAL = "internal"


class Operation(StrEnum):
    """Operations for which callers may need a safe recovery decision."""

    WORKFLOW_TRANSITION = "workflow_transition"
    CALLBACK = "callback"
    PERSISTENCE = "persistence"
    ANALYSIS = "analysis"
    AUTHENTICATION = "authentication"
    JIRA_CREATE = "jira_create"
    JIRA_UPDATE = "jira_update"
    JIRA_ATTACHMENT = "jira_attachment"
    VPN = "vpn"
    TELEGRAM = "telegram"


class Retryability(StrEnum):
    """Whether and how the same logical operation may be retried."""

    NEVER = "never"
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"


class MutationCertainty(StrEnum):
    """Knowledge of an external mutation's outcome."""

    NOT_APPLICABLE = "not_applicable"
    NOT_DISPATCHED = "not_dispatched"
    DEFINITELY_NOT_APPLIED = "definitely_not_applied"
    UNKNOWN = "unknown"
    APPLIED = "applied"


class SafeErrorCode(StrEnum):
    """Fixed user/log-facing codes that contain no provider or user content."""

    INVALID_STATE_TRANSITION = "invalid_state_transition"
    REVISION_CONFLICT = "revision_conflict"
    STATE_CONFLICT = "state_conflict"
    STALE_ACTION = "stale_action"
    VALIDATION_FAILED = "validation_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    CONNECTIVITY_FAILED = "connectivity_failed"
    TIMED_OUT = "timed_out"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_CONTRACT_FAILED = "provider_contract_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    STORAGE_FAILED = "storage_failed"
    INTERNAL_FAILURE = "internal_failure"


MUTATING_OPERATIONS = frozenset(
    {
        Operation.JIRA_CREATE,
        Operation.JIRA_UPDATE,
        Operation.JIRA_ATTACHMENT,
    }
)

_CERTAINLY_RETRYABLE_OUTCOMES = frozenset(
    {
        MutationCertainty.NOT_APPLICABLE,
        MutationCertainty.NOT_DISPATCHED,
        MutationCertainty.DEFINITELY_NOT_APPLIED,
    }
)


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    """A strict recovery classification safe to cross application boundaries."""

    kind: ErrorKind
    operation: Operation
    retryability: Retryability
    mutation_certainty: MutationCertainty
    safe_code: SafeErrorCode

    def __post_init__(self) -> None:
        certainty = self.mutation_certainty
        retryability = self.retryability

        if certainty is not MutationCertainty.NOT_APPLICABLE:
            if self.operation not in MUTATING_OPERATIONS:
                raise ValueError(
                    "external mutation certainty is valid only for Jira mutations"
                )

        if certainty in {MutationCertainty.UNKNOWN, MutationCertainty.APPLIED}:
            if retryability is not Retryability.NEVER:
                raise ValueError(
                    "unknown or applied external mutations cannot be classified as retryable"
                )

        if retryability is not Retryability.NEVER:
            if certainty not in _CERTAINLY_RETRYABLE_OUTCOMES:
                raise ValueError(
                    "retry requires certainty that the external mutation was not applied"
                )

    @property
    def can_retry(self) -> bool:
        """Return whether the same logical operation may be attempted again."""

        return self.retryability is not Retryability.NEVER

    @property
    def requires_reconciliation(self) -> bool:
        """Return whether outcome discovery must precede any recovery action."""

        return self.mutation_certainty is MutationCertainty.UNKNOWN


class DomainError(RuntimeError):
    """Base exception exposing only a stable safe code through ``str``."""

    def __init__(self, classification: ErrorClassification) -> None:
        self.classification = classification
        super().__init__(classification.safe_code.value)


def _state_value(state: object) -> str:
    value = getattr(state, "value", state)
    return value if isinstance(value, str) else type(state).__name__


class InvalidStateTransitionError(DomainError):
    """Raised when the requested workflow transition is never legal."""

    def __init__(self, current_state: object, target_state: object) -> None:
        self.current_state = _state_value(current_state)
        self.target_state = _state_value(target_state)
        super().__init__(
            ErrorClassification(
                kind=ErrorKind.CONFLICT,
                operation=Operation.WORKFLOW_TRANSITION,
                retryability=Retryability.NEVER,
                mutation_certainty=MutationCertainty.NOT_APPLICABLE,
                safe_code=SafeErrorCode.INVALID_STATE_TRANSITION,
            )
        )


class RevisionConflictError(DomainError):
    """Raised when compare-and-swap observes a different workflow revision."""

    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            ErrorClassification(
                kind=ErrorKind.STALE_ACTION,
                operation=Operation.WORKFLOW_TRANSITION,
                retryability=Retryability.NEVER,
                mutation_certainty=MutationCertainty.NOT_APPLICABLE,
                safe_code=SafeErrorCode.REVISION_CONFLICT,
            )
        )


class StateConflictError(DomainError):
    """Raised when compare-and-swap observes a different workflow state."""

    def __init__(self, expected_state: object, actual_state: object) -> None:
        self.expected_state = _state_value(expected_state)
        self.actual_state = _state_value(actual_state)
        super().__init__(
            ErrorClassification(
                kind=ErrorKind.STALE_ACTION,
                operation=Operation.WORKFLOW_TRANSITION,
                retryability=Retryability.NEVER,
                mutation_certainty=MutationCertainty.NOT_APPLICABLE,
                safe_code=SafeErrorCode.STATE_CONFLICT,
            )
        )


class ClassifiedOperationError(DomainError):
    """A provider/storage error already reduced to safe recovery metadata."""


def classify_definite_mutation_failure(
    *,
    operation: Operation,
    kind: ErrorKind,
    safe_code: SafeErrorCode,
    retryability: Retryability = Retryability.EXPLICIT,
    dispatched: bool = True,
) -> ErrorClassification:
    """Classify a mutation known not to have been applied."""

    certainty = (
        MutationCertainty.DEFINITELY_NOT_APPLIED
        if dispatched
        else MutationCertainty.NOT_DISPATCHED
    )
    return ErrorClassification(
        kind=kind,
        operation=operation,
        retryability=retryability,
        mutation_certainty=certainty,
        safe_code=safe_code,
    )


def classify_unknown_mutation_outcome(
    *,
    operation: Operation,
    kind: ErrorKind,
) -> ErrorClassification:
    """Classify a dispatched mutation whose result must be reconciled."""

    return ErrorClassification(
        kind=kind,
        operation=operation,
        retryability=Retryability.NEVER,
        mutation_certainty=MutationCertainty.UNKNOWN,
        safe_code=SafeErrorCode.OUTCOME_UNKNOWN,
    )
