"""Unit tests for DZTGBot canonical domain models and ports."""

from datetime import datetime, timezone
import unittest
import uuid

from dztgbot.domain import (
    AIAnalyzerPort,
    Attachment,
    ClockPort,
    Draft,
    DraftRepositoryPort,
    DraftState,
    IdGeneratorPort,
    JiraGatewayPort,
    JiraTaskTemplate,
    MediaKind,
    PublishedIssue,
    RendererPort,
    RulesRepositoryPort,
    SourceMessageRef,
    SubmissionAttempt,
    TaskSchedulerPort,
    UserRepositoryPort,
    VpnManagerPort,
)


class JiraTaskTemplateTests(unittest.TestCase):
    def test_jira_task_template_creation_and_immutability(self) -> None:
        template = JiraTaskTemplate(
            project_key="NGSSA3",
            issue_type="Task",
            summary="Test summary",
            description="Test description",
            priority="Medium",
            labels=("test", "dev"),
            components=("Backend",),
            assignee="alice",
            acceptance_criteria=["Criterion 1", "Criterion 2"],
        )
        self.assertEqual(template.project_key, "NGSSA3")
        self.assertEqual(template.issue_type, "Task")
        self.assertEqual(template.summary, "Test summary")
        self.assertEqual(template.acceptance_criteria, ["Criterion 1", "Criterion 2"])
        
        # Verify immutability (frozen dataclass)
        with self.assertRaises(AttributeError):
            template.summary = "New summary"  # type: ignore[misc]

    def test_acceptance_criteria_converts_to_list(self) -> None:
        template = JiraTaskTemplate(
            project_key="NGSSA3",
            issue_type="Task",
            summary="Test summary",
            description="Test description",
            priority="Medium",
            acceptance_criteria=("Crit 1", "Crit 2"),  # type: ignore[arg-type]
        )
        self.assertIsInstance(template.acceptance_criteria, list)
        self.assertEqual(template.acceptance_criteria, ["Crit 1", "Crit 2"])


class SourceMessageRefTests(unittest.TestCase):
    def test_valid_source_message_ref(self) -> None:
        msg = SourceMessageRef(
            message_id=101,
            chat_id=-100123456789,
            sender_id=999,
            text="Hello world",
            media_kind=MediaKind.TEXT,
        )
        self.assertEqual(msg.message_id, 101)
        self.assertEqual(msg.chat_id, -100123456789)
        self.assertEqual(msg.sender_id, 999)
        self.assertIsNotNone(msg.received_at.tzinfo)

    def test_invalid_source_message_ref(self) -> None:
        with self.assertRaises(ValueError):
            SourceMessageRef(message_id=0, chat_id=-100123, sender_id=999)
        with self.assertRaises(ValueError):
            SourceMessageRef(message_id=101, chat_id=0, sender_id=999)
        with self.assertRaises(ValueError):
            SourceMessageRef(message_id=101, chat_id=-100123, sender_id=0)
        with self.assertRaises(ValueError):
            SourceMessageRef(
                message_id=101,
                chat_id=-100123,
                sender_id=999,
                received_at=datetime.now(),  # Naive datetime
            )


class AttachmentTests(unittest.TestCase):
    def test_valid_attachment(self) -> None:
        att = Attachment(
            file_id="AgACAg123",
            file_unique_id="AQAD987",
            media_kind=MediaKind.PHOTO,
            file_name="photo.jpg",
            file_size=1024,
        )
        self.assertEqual(att.file_id, "AgACAg123")
        self.assertEqual(att.file_unique_id, "AQAD987")
        self.assertEqual(att.file_size, 1024)

    def test_empty_attachment_ids_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Attachment(file_id="", file_unique_id="AQAD987")
        with self.assertRaises(ValueError):
            Attachment(file_id="AgACAg123", file_unique_id="")


class SubmissionAttemptTests(unittest.TestCase):
    def test_valid_submission_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        attempt = SubmissionAttempt(
            attempt_id="att-1",
            draft_id="dft-1",
            request_hash="abc123hash",
            attempt_number=1,
            started_at=now,
            status="pending",
        )
        self.assertEqual(attempt.attempt_id, "att-1")
        self.assertEqual(attempt.attempt_number, 1)
        self.assertEqual(attempt.status, "pending")

    def test_invalid_submission_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            SubmissionAttempt(
                attempt_id="",
                draft_id="dft-1",
                request_hash="hash",
                attempt_number=1,
                started_at=now,
            )
        with self.assertRaises(ValueError):
            SubmissionAttempt(
                attempt_id="att-1",
                draft_id="dft-1",
                request_hash="hash",
                attempt_number=0,
                started_at=now,
            )


class PublishedIssueTests(unittest.TestCase):
    def test_valid_published_issue(self) -> None:
        pub = PublishedIssue(
            issue_key="NGSSA3-100",
            issue_id="10001",
            issue_url="https://jira.example.com/browse/NGSSA3-100",
        )
        self.assertEqual(pub.issue_key, "NGSSA3-100")
        self.assertEqual(pub.issue_id, "10001")
        self.assertIsNotNone(pub.published_at.tzinfo)

    def test_empty_issue_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PublishedIssue(issue_key="", issue_id="100", issue_url="https://example.com")


class DraftTests(unittest.TestCase):
    def test_draft_factory_creation(self) -> None:
        draft = Draft.create_new(owner_id=12345, chat_id=12345)
        self.assertTrue(len(draft.draft_id) > 0)
        self.assertEqual(draft.owner_id, 12345)
        self.assertEqual(draft.chat_id, 12345)
        self.assertEqual(draft.state, DraftState.COLLECTING)
        self.assertEqual(draft.revision, 1)
        self.assertIsNotNone(draft.created_at.tzinfo)
        self.assertIsNotNone(draft.updated_at.tzinfo)

    def test_draft_state_is_canonical_fsm_enum(self) -> None:
        """Package DraftState must be the full FSM enum, not a reduced models copy."""
        from dztgbot.domain.fsm import DraftState as FsmDraftState

        self.assertIs(DraftState, FsmDraftState)
        self.assertEqual(DraftState.REVIEW.value, "review")
        self.assertEqual(DraftState.SUBMISSION_RETRYABLE.value, "submission_retryable")
        self.assertFalse(hasattr(DraftState, "REVIEWING"))
        self.assertFalse(hasattr(DraftState, "FAILED_RETRYABLE"))
        draft = Draft.create_new(owner_id=1, chat_id=1)
        self.assertIsInstance(draft.state, FsmDraftState)

    def test_invalid_draft_owner_and_chat(self) -> None:
        with self.assertRaises(ValueError):
            Draft.create_new(owner_id=0, chat_id=12345)
        with self.assertRaises(ValueError):
            Draft.create_new(owner_id=12345, chat_id=0)

    def test_draft_immutability(self) -> None:
        draft = Draft.create_new(owner_id=12345, chat_id=12345)
        with self.assertRaises(AttributeError):
            draft.state = DraftState.REVIEW  # type: ignore[misc]


class ProtocolComplianceTests(unittest.TestCase):
    def test_mock_clock_protocol_compliance(self) -> None:
        class MockClock:
            def now(self) -> datetime:
                return datetime.now(timezone.utc)

        clock: ClockPort = MockClock()
        self.assertIsNotNone(clock.now().tzinfo)

    def test_mock_id_generator_protocol_compliance(self) -> None:
        class MockIdGenerator:
            def generate_uuid(self) -> str:
                return str(uuid.uuid4())

            def generate_opaque_token(self, length_bytes: int = 16) -> str:
                return "0123456789abcdef0123456789abcdef"

        gen: IdGeneratorPort = MockIdGenerator()
        self.assertEqual(len(gen.generate_opaque_token()), 32)


if __name__ == "__main__":
    unittest.main()
