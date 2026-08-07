from __future__ import annotations

import unittest

from dztgbot.domain.errors import (
    ClassifiedOperationError,
    ErrorClassification,
    ErrorKind,
    InvalidStateTransitionError,
    MutationCertainty,
    Operation,
    Retryability,
    SafeErrorCode,
    classify_definite_mutation_failure,
    classify_unknown_mutation_outcome,
)


class ErrorClassificationTests(unittest.TestCase):
    def test_non_mutating_failure_can_be_automatically_retryable(self) -> None:
        classification = ErrorClassification(
            kind=ErrorKind.RATE_LIMIT,
            operation=Operation.ANALYSIS,
            retryability=Retryability.AUTOMATIC,
            mutation_certainty=MutationCertainty.NOT_APPLICABLE,
            safe_code=SafeErrorCode.RATE_LIMITED,
        )
        self.assertTrue(classification.can_retry)
        self.assertFalse(classification.requires_reconciliation)

    def test_unknown_mutation_is_never_retryable_and_requires_reconciliation(self) -> None:
        classification = classify_unknown_mutation_outcome(
            operation=Operation.JIRA_CREATE,
            kind=ErrorKind.TIMEOUT,
        )
        self.assertEqual(classification.retryability, Retryability.NEVER)
        self.assertEqual(
            classification.mutation_certainty,
            MutationCertainty.UNKNOWN,
        )
        self.assertEqual(classification.safe_code, SafeErrorCode.OUTCOME_UNKNOWN)
        self.assertFalse(classification.can_retry)
        self.assertTrue(classification.requires_reconciliation)

    def test_unknown_or_applied_mutation_cannot_be_classified_as_retryable(self) -> None:
        for certainty in (MutationCertainty.UNKNOWN, MutationCertainty.APPLIED):
            for retryability in (Retryability.AUTOMATIC, Retryability.EXPLICIT):
                with self.subTest(certainty=certainty, retryability=retryability):
                    with self.assertRaises(ValueError):
                        ErrorClassification(
                            kind=ErrorKind.TIMEOUT,
                            operation=Operation.JIRA_CREATE,
                            retryability=retryability,
                            mutation_certainty=certainty,
                            safe_code=SafeErrorCode.OUTCOME_UNKNOWN,
                        )

    def test_non_mutating_operation_cannot_claim_external_mutation_certainty(self) -> None:
        for certainty in (
            MutationCertainty.NOT_DISPATCHED,
            MutationCertainty.DEFINITELY_NOT_APPLIED,
            MutationCertainty.UNKNOWN,
            MutationCertainty.APPLIED,
        ):
            with self.subTest(certainty=certainty):
                with self.assertRaises(ValueError):
                    ErrorClassification(
                        kind=ErrorKind.INTERNAL,
                        operation=Operation.ANALYSIS,
                        retryability=Retryability.NEVER,
                        mutation_certainty=certainty,
                        safe_code=SafeErrorCode.INTERNAL_FAILURE,
                    )

    def test_definite_failure_factory_separates_dispatch_and_retry_policy(self) -> None:
        dispatched = classify_definite_mutation_failure(
            operation=Operation.JIRA_UPDATE,
            kind=ErrorKind.PROVIDER_REJECTION,
            safe_code=SafeErrorCode.PROVIDER_REJECTED,
        )
        not_dispatched = classify_definite_mutation_failure(
            operation=Operation.JIRA_UPDATE,
            kind=ErrorKind.CONNECTIVITY,
            safe_code=SafeErrorCode.CONNECTIVITY_FAILED,
            retryability=Retryability.AUTOMATIC,
            dispatched=False,
        )

        self.assertEqual(
            dispatched.mutation_certainty,
            MutationCertainty.DEFINITELY_NOT_APPLIED,
        )
        self.assertEqual(dispatched.retryability, Retryability.EXPLICIT)
        self.assertEqual(
            not_dispatched.mutation_certainty,
            MutationCertainty.NOT_DISPATCHED,
        )
        self.assertEqual(not_dispatched.retryability, Retryability.AUTOMATIC)

    def test_mutation_factories_reject_non_mutating_operations(self) -> None:
        with self.assertRaises(ValueError):
            classify_definite_mutation_failure(
                operation=Operation.ANALYSIS,
                kind=ErrorKind.TIMEOUT,
                safe_code=SafeErrorCode.TIMED_OUT,
            )
        with self.assertRaises(ValueError):
            classify_unknown_mutation_outcome(
                operation=Operation.PERSISTENCE,
                kind=ErrorKind.STORAGE,
            )

    def test_classified_exception_exposes_only_safe_code_in_message(self) -> None:
        classification = ErrorClassification(
            kind=ErrorKind.STORAGE,
            operation=Operation.PERSISTENCE,
            retryability=Retryability.EXPLICIT,
            mutation_certainty=MutationCertainty.NOT_APPLICABLE,
            safe_code=SafeErrorCode.STORAGE_FAILED,
        )
        error = ClassifiedOperationError(classification)
        self.assertEqual(str(error), "storage_failed")
        self.assertNotIn("provider", str(error))

    def test_transition_exception_is_non_retryable_and_content_free(self) -> None:
        error = InvalidStateTransitionError("review", "created")
        self.assertEqual(str(error), "invalid_state_transition")
        self.assertEqual(error.current_state, "review")
        self.assertEqual(error.target_state, "created")
        self.assertEqual(error.classification.kind, ErrorKind.CONFLICT)
        self.assertEqual(error.classification.retryability, Retryability.NEVER)
        self.assertEqual(
            error.classification.mutation_certainty,
            MutationCertainty.NOT_APPLICABLE,
        )


if __name__ == "__main__":
    unittest.main()
