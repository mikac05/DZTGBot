"""Regression tests for the temporary Phase 5 provider facades."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import unittest

from dztgbot.analysis import JiraTaskTemplate as LegacyTemplate
from dztgbot.domain.errors import (
    ErrorKind,
    MutationCertainty,
    Operation,
    SafeErrorCode,
    classify_unknown_mutation_outcome,
)
from dztgbot.domain.models import JiraTaskTemplate, PublishedIssue
from dztgbot.infrastructure.jira_gateway import JiraGatewayError
from dztgbot.jira_client import JiraClient, JiraClientError


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def legacy_template(issue_type: str = "缺陷") -> LegacyTemplate:
    return LegacyTemplate(
        summary="summary",
        description="description",
        issuetype=issue_type,
        labels=["telegram-intake"],
        priority="High",
        project_key="BOT",
        components=["api"],
        assignee="owner",
        acceptance_criteria=["works"],
    )


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.create_error: Exception | None = None
        self.closed = False

    async def test_credential(self, pat: str) -> bool:
        self.calls.append(("test", pat))
        return True

    async def create_issue(
        self, template: JiraTaskTemplate, pat: str, idempotency_key: str | None = None
    ) -> PublishedIssue:
        self.calls.append(("create", template, pat, idempotency_key))
        if self.create_error is not None:
            raise self.create_error
        return PublishedIssue("BOT-1", "10001", "https://jira.example/browse/BOT-1", NOW)

    async def update_issue(
        self, issue_key: str, template: JiraTaskTemplate, pat: str
    ) -> None:
        self.calls.append(("update", issue_key, template, pat))

    async def upload_attachment(
        self,
        issue_key: str,
        filename: str,
        content: bytes,
        mime_type: str,
        pat: str,
    ) -> str:
        self.calls.append(("attachment", issue_key, filename, content, mime_type, pat))
        return "attachment-1"

    async def aclose(self) -> None:
        self.closed = True


class LegacyTemplateFacadeTests(unittest.TestCase):
    def test_legacy_template_is_canonical_without_field_drift(self) -> None:
        template = legacy_template()
        self.assertIsInstance(template, JiraTaskTemplate)
        self.assertEqual(template.issuetype, template.issue_type)
        self.assertEqual(template.labels, ("telegram-intake",))
        self.assertEqual(
            [field.name for field in fields(template)],
            [field.name for field in fields(JiraTaskTemplate)],
        )


class LegacyJiraFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_legacy_operations_delegate_to_one_gateway(self) -> None:
        gateway = RecordingGateway()
        client = JiraClient(base_url="https://jira.example", gateway=gateway)  # type: ignore[arg-type]
        template = legacy_template()

        await client.validate_credentials("pat-one")
        created = await client.create_issue("pat-one", template)
        updated = await client.update_issue("pat-one", "BOT-1", template)
        await client.add_attachment("pat-one", "BOT-1", "a.jpg", b"bytes")

        self.assertEqual(created.key, "BOT-1")
        self.assertEqual(updated.key, "BOT-1")
        self.assertEqual([call[0] for call in gateway.calls], ["test", "create", "update", "attachment"])
        self.assertEqual(gateway.calls[0][-1], "pat-one")
        self.assertEqual(gateway.calls[1][-2], "pat-one")
        self.assertEqual(gateway.calls[2][-1], "pat-one")
        self.assertEqual(gateway.calls[3][-1], "pat-one")
        await client.aclose()
        self.assertTrue(gateway.closed)

    async def test_unknown_create_error_remains_safe_typed_and_not_retried(self) -> None:
        gateway = RecordingGateway()
        gateway.create_error = JiraGatewayError(
            classify_unknown_mutation_outcome(
                operation=Operation.JIRA_CREATE,
                kind=ErrorKind.TIMEOUT,
            )
        )
        client = JiraClient(base_url="https://jira.example", gateway=gateway)  # type: ignore[arg-type]

        with self.assertRaises(JiraClientError) as raised:
            await client.create_issue("pat-one", legacy_template("UnknownCustomType"))

        error = raised.exception
        self.assertEqual(str(error), SafeErrorCode.OUTCOME_UNKNOWN.value)
        self.assertEqual(error.classification.mutation_certainty, MutationCertainty.UNKNOWN)
        self.assertEqual([call[0] for call in gateway.calls], ["create"])

    async def test_invalid_pat_shape_is_a_safe_typed_validation_error(self) -> None:
        client = JiraClient(base_url="https://jira.example")
        try:
            with self.assertRaises(JiraClientError) as raised:
                await client.create_issue("Basic unsafe", legacy_template())
            self.assertEqual(str(raised.exception), SafeErrorCode.VALIDATION_FAILED.value)
            self.assertEqual(raised.exception.classification.kind, ErrorKind.VALIDATION)
            self.assertEqual(
                raised.exception.classification.mutation_certainty,
                MutationCertainty.NOT_DISPATCHED,
            )
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
