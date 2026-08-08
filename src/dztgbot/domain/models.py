"""Canonical domain entities and value objects for DZTGBot.

``DraftState`` is defined only in ``domain.fsm`` (single source of truth for the
full lifecycle). Entities import that enum; do not reintroduce a second state
enum here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import uuid

from .fsm import DraftState


class MediaKind(str, Enum):
    """Supported media types for intake and attachments."""
    TEXT = "text"
    PHOTO = "photo"
    DOCUMENT = "document"
    VIDEO = "video"
    VOICE = "voice"


@dataclass(frozen=True)
class JiraTaskTemplate:
    """Canonical domain representation of a Jira task template."""
    project_key: str
    issue_type: str
    summary: str
    description: str
    priority: str
    labels: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    assignee: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.acceptance_criteria, list):
            object.__setattr__(self, "acceptance_criteria", list(self.acceptance_criteria))


@dataclass(frozen=True)
class SourceMessageRef:
    """Reference to a source Telegram message included in a draft batch."""
    message_id: int
    chat_id: int
    sender_id: int
    text: str = ""
    media_kind: MediaKind = MediaKind.TEXT
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.message_id <= 0:
            raise ValueError(f"message_id must be positive, got {self.message_id}")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be 0")
        if self.sender_id <= 0:
            raise ValueError(f"sender_id must be positive, got {self.sender_id}")
        if self.received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware (UTC)")


@dataclass(frozen=True)
class Attachment:
    """Attachment reference for photos and media files."""
    file_id: str
    file_unique_id: str
    media_kind: MediaKind = MediaKind.PHOTO
    file_name: str | None = None
    file_size: int | None = None
    uploaded_attachment_id: str | None = None

    def __post_init__(self) -> None:
        if not self.file_id or not self.file_unique_id:
            raise ValueError("file_id and file_unique_id must not be empty")


@dataclass(frozen=True)
class SubmissionAttempt:
    """Recorded submission attempt before/after Jira dispatch."""
    attempt_id: str
    draft_id: str
    request_hash: str
    attempt_number: int
    started_at: datetime
    completed_at: datetime | None = None
    status: str = "pending"  # pending, success, failed, unknown
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not self.attempt_id or not self.draft_id:
            raise ValueError("attempt_id and draft_id must not be empty")
        if self.attempt_number <= 0:
            raise ValueError("attempt_number must be positive")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware (UTC)")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware (UTC)")


@dataclass(frozen=True)
class PublishedIssue:
    """Metadata for a successfully published Jira issue."""
    issue_key: str
    issue_id: str
    issue_url: str
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.issue_key or not self.issue_url:
            raise ValueError("issue_key and issue_url must not be empty")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware (UTC)")


@dataclass(frozen=True)
class Draft:
    """Aggregate root representing a user's Jira issue draft."""
    draft_id: str
    owner_id: int
    chat_id: int
    message_thread_id: int | None = None
    state: DraftState = DraftState.COLLECTING
    revision: int = 1
    template: JiraTaskTemplate | None = None
    source_messages: tuple[SourceMessageRef, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    published_issue: PublishedIssue | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.draft_id:
            raise ValueError("draft_id must not be empty")
        if self.owner_id <= 0:
            raise ValueError(f"owner_id must be a positive integer, got {self.owner_id}")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be 0")
        if self.revision < 1:
            raise ValueError(f"revision must be >= 1, got {self.revision}")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("created_at and updated_at must be timezone-aware (UTC)")

    @classmethod
    def create_new(
        cls,
        owner_id: int,
        chat_id: int,
        message_thread_id: int | None = None,
        draft_id: str | None = None,
    ) -> Draft:
        """Factory method to create a new draft with default UUID and UTC timestamp."""
        now = datetime.now(timezone.utc)
        return cls(
            draft_id=draft_id or str(uuid.uuid4()),
            owner_id=owner_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            state=DraftState.COLLECTING,
            revision=1,
            created_at=now,
            updated_at=now,
        )


@dataclass(frozen=True)
class JiraTransitionView:
    """Canonical domain view of an available Jira issue transition."""
    transition_id: str
    name: str
    to_status: str
    has_screen: bool = False

    def __post_init__(self) -> None:
        if not self.transition_id or not self.name or not self.to_status:
            raise ValueError("transition_id, name, and to_status must not be empty")


@dataclass(frozen=True)
class JiraIssueView:
    """Canonical domain view of a live Jira issue for triage and rendering."""
    issue_key: str
    issue_id: str
    summary: str
    status: str
    priority: str
    assignee: str = ""
    reporter: str = ""
    epic_key: str = ""
    epic_name: str = ""
    sprint_name: str = ""
    is_flagged: bool = False
    blocker_keys: tuple[str, ...] = ()
    description: str = ""
    issue_url: str = ""
    last_comment_summary: str = ""
    updated_at: str = ""
    is_watching: bool = False

    def __post_init__(self) -> None:
        if not self.issue_key or not self.issue_id:
            raise ValueError("issue_key and issue_id must not be empty")


@dataclass(frozen=True)
class JiraSearchResult:
    """Result container for JQL search operations."""
    total: int
    issues: tuple[JiraIssueView, ...]
    jql: str

