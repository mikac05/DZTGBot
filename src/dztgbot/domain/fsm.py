"""Complete workflow state machine and compare-and-swap transition types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Final, Mapping

from .errors import (
    InvalidStateTransitionError,
    RevisionConflictError,
    StateConflictError,
)


class DraftState(StrEnum):
    """Every durable state in the draft, submission, and published-edit lifecycle."""

    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    ANALYSIS_FAILED = "analysis_failed"
    REVIEW = "review"
    EDITING = "editing"
    SUBMITTING = "submitting"
    SUBMISSION_RETRYABLE = "submission_retryable"
    SUBMISSION_UNKNOWN = "submission_unknown"
    CREATED = "created"
    ATTACHING = "attaching"
    ATTACHMENT_PARTIAL = "attachment_partial"
    COMPLETE = "complete"
    UPDATE_REVIEW = "update_review"
    UPDATING = "updating"
    UPDATE_RETRYABLE = "update_retryable"
    UPDATE_UNKNOWN = "update_unknown"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ABANDONED_UNKNOWN = "abandoned_unknown"


_TRANSITIONS: dict[DraftState, frozenset[DraftState]] = {
    DraftState.COLLECTING: frozenset(
        {
            DraftState.ANALYZING,
            DraftState.REVIEW,
            DraftState.CANCELLED,
            DraftState.EXPIRED,
        }
    ),
    DraftState.ANALYZING: frozenset(
        {
            DraftState.REVIEW,
            DraftState.ANALYSIS_FAILED,
            DraftState.CANCELLED,
        }
    ),
    DraftState.ANALYSIS_FAILED: frozenset(
        {
            DraftState.ANALYZING,
            DraftState.CANCELLED,
            DraftState.EXPIRED,
        }
    ),
    DraftState.REVIEW: frozenset(
        {
            DraftState.EDITING,
            DraftState.SUBMITTING,
            DraftState.CANCELLED,
            DraftState.EXPIRED,
        }
    ),
    DraftState.EDITING: frozenset(
        {
            DraftState.REVIEW,
            DraftState.CANCELLED,
            DraftState.EXPIRED,
        }
    ),
    DraftState.SUBMITTING: frozenset(
        {
            DraftState.CREATED,
            DraftState.SUBMISSION_RETRYABLE,
            DraftState.SUBMISSION_UNKNOWN,
        }
    ),
    DraftState.SUBMISSION_RETRYABLE: frozenset(
        {
            DraftState.SUBMITTING,
            DraftState.EDITING,
            DraftState.CANCELLED,
            DraftState.EXPIRED,
        }
    ),
    DraftState.SUBMISSION_UNKNOWN: frozenset(
        {
            DraftState.CREATED,
            DraftState.SUBMISSION_RETRYABLE,
            DraftState.ABANDONED_UNKNOWN,
        }
    ),
    DraftState.CREATED: frozenset(
        {
            DraftState.ATTACHING,
            DraftState.COMPLETE,
        }
    ),
    DraftState.ATTACHING: frozenset(
        {
            DraftState.COMPLETE,
            DraftState.ATTACHMENT_PARTIAL,
        }
    ),
    DraftState.ATTACHMENT_PARTIAL: frozenset(
        {
            DraftState.ATTACHING,
            DraftState.COMPLETE,
            DraftState.UPDATE_REVIEW,
        }
    ),
    DraftState.COMPLETE: frozenset({DraftState.UPDATE_REVIEW}),
    DraftState.UPDATE_REVIEW: frozenset(
        {
            DraftState.UPDATING,
            DraftState.COMPLETE,
        }
    ),
    DraftState.UPDATING: frozenset(
        {
            DraftState.COMPLETE,
            DraftState.UPDATE_RETRYABLE,
            DraftState.UPDATE_UNKNOWN,
        }
    ),
    DraftState.UPDATE_RETRYABLE: frozenset(
        {
            DraftState.UPDATING,
            DraftState.UPDATE_REVIEW,
            DraftState.COMPLETE,
        }
    ),
    DraftState.UPDATE_UNKNOWN: frozenset(
        {
            DraftState.COMPLETE,
            DraftState.UPDATE_RETRYABLE,
            DraftState.ABANDONED_UNKNOWN,
        }
    ),
    DraftState.CANCELLED: frozenset(),
    DraftState.EXPIRED: frozenset(),
    DraftState.ABANDONED_UNKNOWN: frozenset(),
}

LEGAL_TRANSITIONS: Final[Mapping[DraftState, frozenset[DraftState]]] = (
    MappingProxyType(_TRANSITIONS)
)

UNKNOWN_OUTCOME_STATES: Final[frozenset[DraftState]] = frozenset(
    {
        DraftState.SUBMISSION_UNKNOWN,
        DraftState.UPDATE_UNKNOWN,
        DraftState.ABANDONED_UNKNOWN,
    }
)

EXPIRABLE_STATES: Final[frozenset[DraftState]] = frozenset(
    {
        DraftState.COLLECTING,
        DraftState.ANALYSIS_FAILED,
        DraftState.REVIEW,
        DraftState.EDITING,
        DraftState.SUBMISSION_RETRYABLE,
    }
)

TERMINAL_STATES: Final[frozenset[DraftState]] = frozenset(
    {
        DraftState.CANCELLED,
        DraftState.EXPIRED,
        DraftState.ABANDONED_UNKNOWN,
    }
)

_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class TransitionCommand:
    """A compare-and-swap request against one expected workflow version."""

    workflow_id: str
    expected_revision: int
    expected_state: DraftState
    target_state: DraftState
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.workflow_id or len(self.workflow_id) > 128:
            raise ValueError("workflow_id must contain 1 to 128 characters")
        if any(character.isspace() or ord(character) < 32 for character in self.workflow_id):
            raise ValueError("workflow_id must not contain whitespace or control characters")
        if self.expected_revision < 1:
            raise ValueError("expected_revision must be at least 1")
        if not isinstance(self.expected_state, DraftState):
            raise TypeError("expected_state must be a DraftState")
        if not isinstance(self.target_state, DraftState):
            raise TypeError("target_state must be a DraftState")
        if self.reason_code is not None and not _REASON_CODE.fullmatch(
            self.reason_code
        ):
            raise ValueError("reason_code must be a safe lowercase code")


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """The deterministic state/revision result of a successful CAS transition."""

    workflow_id: str
    previous_state: DraftState
    current_state: DraftState
    previous_revision: int
    current_revision: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.previous_revision < 1:
            raise ValueError("previous_revision must be at least 1")
        if self.current_revision != self.previous_revision + 1:
            raise ValueError("current_revision must increment exactly once")


def allowed_targets(state: DraftState) -> frozenset[DraftState]:
    """Return the immutable set of legal targets from ``state``."""

    return LEGAL_TRANSITIONS[state]


def is_transition_allowed(current: DraftState, target: DraftState) -> bool:
    """Return whether the state machine permits ``current -> target``."""

    return target in LEGAL_TRANSITIONS[current]


def validate_transition(current: DraftState, target: DraftState) -> None:
    """Raise a typed safe error when ``current -> target`` is illegal."""

    if not is_transition_allowed(current, target):
        raise InvalidStateTransitionError(current, target)


def is_expirable(state: DraftState) -> bool:
    """Return whether ordinary workflow expiry may act on this state."""

    return state in EXPIRABLE_STATES


def requires_reconciliation(state: DraftState) -> bool:
    """Return whether normal mutation/retry paths must remain disabled."""

    return state in UNKNOWN_OUTCOME_STATES


def evaluate_transition(
    command: TransitionCommand,
    *,
    actual_state: DraftState,
    actual_revision: int,
) -> TransitionResult:
    """Evaluate a transition against current state without mutating storage.

    Repositories can call this function inside a short transaction, persist the
    returned state and revision, and then release the transaction before I/O.
    """

    if actual_revision != command.expected_revision:
        raise RevisionConflictError(command.expected_revision, actual_revision)
    if actual_state is not command.expected_state:
        raise StateConflictError(command.expected_state, actual_state)

    validate_transition(actual_state, command.target_state)
    return TransitionResult(
        workflow_id=command.workflow_id,
        previous_state=actual_state,
        current_state=command.target_state,
        previous_revision=actual_revision,
        current_revision=actual_revision + 1,
        reason_code=command.reason_code,
    )
