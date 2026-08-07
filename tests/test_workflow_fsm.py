from __future__ import annotations

import itertools
import unittest

from dztgbot.domain.errors import (
    InvalidStateTransitionError,
    RevisionConflictError,
    SafeErrorCode,
    StateConflictError,
)
from dztgbot.domain.fsm import (
    EXPIRABLE_STATES,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    UNKNOWN_OUTCOME_STATES,
    DraftState,
    TransitionCommand,
    TransitionResult,
    allowed_targets,
    evaluate_transition,
    is_expirable,
    is_transition_allowed,
    requires_reconciliation,
    validate_transition,
)


class TransitionMatrixTests(unittest.TestCase):
    def test_transition_table_has_exactly_one_entry_for_every_state(self) -> None:
        self.assertEqual(set(LEGAL_TRANSITIONS), set(DraftState))
        self.assertTrue(
            all(isinstance(targets, frozenset) for targets in LEGAL_TRANSITIONS.values())
        )

    def test_every_state_pair_is_accepted_or_rejected_by_the_table(self) -> None:
        for current, target in itertools.product(DraftState, repeat=2):
            with self.subTest(current=current, target=target):
                expected = target in LEGAL_TRANSITIONS[current]
                self.assertEqual(is_transition_allowed(current, target), expected)
                self.assertEqual(target in allowed_targets(current), expected)

                if expected:
                    validate_transition(current, target)
                else:
                    with self.assertRaises(InvalidStateTransitionError) as captured:
                        validate_transition(current, target)
                    self.assertEqual(
                        captured.exception.classification.safe_code,
                        SafeErrorCode.INVALID_STATE_TRANSITION,
                    )

    def test_no_state_can_transition_to_itself(self) -> None:
        for state in DraftState:
            with self.subTest(state=state):
                self.assertFalse(is_transition_allowed(state, state))

    def test_submission_unknown_has_only_reconciliation_outcomes(self) -> None:
        self.assertEqual(
            allowed_targets(DraftState.SUBMISSION_UNKNOWN),
            frozenset(
                {
                    DraftState.CREATED,
                    DraftState.SUBMISSION_RETRYABLE,
                    DraftState.ABANDONED_UNKNOWN,
                }
            ),
        )
        self.assertFalse(is_expirable(DraftState.SUBMISSION_UNKNOWN))
        self.assertNotIn(
            DraftState.CANCELLED,
            allowed_targets(DraftState.SUBMISSION_UNKNOWN),
        )
        self.assertNotIn(
            DraftState.SUBMITTING,
            allowed_targets(DraftState.SUBMISSION_UNKNOWN),
        )

    def test_update_unknown_has_only_reconciliation_outcomes(self) -> None:
        self.assertEqual(
            allowed_targets(DraftState.UPDATE_UNKNOWN),
            frozenset(
                {
                    DraftState.COMPLETE,
                    DraftState.UPDATE_RETRYABLE,
                    DraftState.ABANDONED_UNKNOWN,
                }
            ),
        )
        self.assertFalse(is_expirable(DraftState.UPDATE_UNKNOWN))
        self.assertNotIn(
            DraftState.UPDATING,
            allowed_targets(DraftState.UPDATE_UNKNOWN),
        )

    def test_unknown_states_require_reconciliation_and_never_expire(self) -> None:
        self.assertEqual(
            UNKNOWN_OUTCOME_STATES,
            frozenset(
                {
                    DraftState.SUBMISSION_UNKNOWN,
                    DraftState.UPDATE_UNKNOWN,
                    DraftState.ABANDONED_UNKNOWN,
                }
            ),
        )
        for state in DraftState:
            with self.subTest(state=state):
                self.assertEqual(
                    requires_reconciliation(state),
                    state in UNKNOWN_OUTCOME_STATES,
                )
                if state in UNKNOWN_OUTCOME_STATES:
                    self.assertFalse(is_expirable(state))

    def test_expirable_states_are_exact_and_can_reach_expired(self) -> None:
        self.assertEqual(
            EXPIRABLE_STATES,
            frozenset(
                {
                    DraftState.COLLECTING,
                    DraftState.ANALYSIS_FAILED,
                    DraftState.REVIEW,
                    DraftState.EDITING,
                    DraftState.SUBMISSION_RETRYABLE,
                }
            ),
        )
        for state in DraftState:
            with self.subTest(state=state):
                self.assertEqual(is_expirable(state), state in EXPIRABLE_STATES)
                self.assertEqual(
                    DraftState.EXPIRED in allowed_targets(state),
                    state in EXPIRABLE_STATES,
                )

    def test_terminal_states_have_no_outgoing_transition(self) -> None:
        self.assertEqual(
            TERMINAL_STATES,
            frozenset(
                {
                    DraftState.CANCELLED,
                    DraftState.EXPIRED,
                    DraftState.ABANDONED_UNKNOWN,
                }
            ),
        )
        for state in TERMINAL_STATES:
            with self.subTest(state=state):
                self.assertEqual(allowed_targets(state), frozenset())


class CompareAndSwapTests(unittest.TestCase):
    @staticmethod
    def command(**overrides: object) -> TransitionCommand:
        values: dict[str, object] = {
            "workflow_id": "workflow-123",
            "expected_revision": 7,
            "expected_state": DraftState.REVIEW,
            "target_state": DraftState.SUBMITTING,
            "reason_code": "user.confirmed",
        }
        values.update(overrides)
        return TransitionCommand(**values)  # type: ignore[arg-type]

    def test_successful_transition_increments_revision_exactly_once(self) -> None:
        result = evaluate_transition(
            self.command(),
            actual_state=DraftState.REVIEW,
            actual_revision=7,
        )
        self.assertEqual(
            result,
            TransitionResult(
                workflow_id="workflow-123",
                previous_state=DraftState.REVIEW,
                current_state=DraftState.SUBMITTING,
                previous_revision=7,
                current_revision=8,
                reason_code="user.confirmed",
            ),
        )

    def test_revision_conflict_fails_before_transition(self) -> None:
        with self.assertRaises(RevisionConflictError) as captured:
            evaluate_transition(
                self.command(),
                actual_state=DraftState.REVIEW,
                actual_revision=8,
            )
        self.assertEqual(captured.exception.expected_revision, 7)
        self.assertEqual(captured.exception.actual_revision, 8)
        self.assertEqual(
            captured.exception.classification.safe_code,
            SafeErrorCode.REVISION_CONFLICT,
        )

    def test_state_conflict_fails_before_transition(self) -> None:
        with self.assertRaises(StateConflictError) as captured:
            evaluate_transition(
                self.command(),
                actual_state=DraftState.EDITING,
                actual_revision=7,
            )
        self.assertEqual(captured.exception.expected_state, DraftState.REVIEW.value)
        self.assertEqual(captured.exception.actual_state, DraftState.EDITING.value)

    def test_illegal_transition_raises_typed_error(self) -> None:
        command = self.command(target_state=DraftState.CREATED)
        with self.assertRaises(InvalidStateTransitionError):
            evaluate_transition(
                command,
                actual_state=DraftState.REVIEW,
                actual_revision=7,
            )

    def test_command_rejects_invalid_identity_revision_state_and_reason(self) -> None:
        invalid_cases = (
            {"workflow_id": ""},
            {"workflow_id": "contains whitespace"},
            {"workflow_id": "x" * 129},
            {"expected_revision": 0},
            {"expected_state": "review"},
            {"target_state": "submitting"},
            {"reason_code": "Unsafe Message"},
            {"reason_code": "x" * 65},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises((TypeError, ValueError)):
                    self.command(**overrides)

    def test_result_requires_exactly_one_revision_increment(self) -> None:
        for current_revision in (7, 9):
            with self.subTest(current_revision=current_revision):
                with self.assertRaises(ValueError):
                    TransitionResult(
                        workflow_id="workflow-123",
                        previous_state=DraftState.REVIEW,
                        current_state=DraftState.SUBMITTING,
                        previous_revision=7,
                        current_revision=current_revision,
                    )


if __name__ == "__main__":
    unittest.main()
