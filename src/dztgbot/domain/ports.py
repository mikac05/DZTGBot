"""Pure Python domain port protocols for DZTGBot."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, Sequence

from .fsm import DraftState
from .models import (
    Attachment,
    Draft,
    JiraIssueView,
    JiraSearchResult,
    JiraTaskTemplate,
    JiraTransitionView,
    PublishedIssue,
    SourceMessageRef,
    SubmissionAttempt,
)


class ClockPort(Protocol):
    """Port for retrieving the current UTC datetime."""

    def now(self) -> datetime:
        ...


class IdGeneratorPort(Protocol):
    """Port for generating unique IDs and secure tokens."""

    def generate_uuid(self) -> str:
        ...

    def generate_opaque_token(self, length_bytes: int = 16) -> str:
        ...


class DraftRepositoryPort(Protocol):
    """Port for persisting and querying Draft aggregate roots."""

    async def save(self, draft: Draft) -> None:
        ...

    async def get_by_id(self, draft_id: str) -> Draft | None:
        ...

    async def compare_and_swap_state(
        self,
        draft_id: str,
        expected_revision: int,
        target_state: DraftState,
        last_error: str | None = None,
    ) -> Draft:
        ...

    async def record_attempt(self, attempt: SubmissionAttempt) -> None:
        ...

    async def update_attempt(self, attempt: SubmissionAttempt) -> None:
        ...

    async def get_latest_attempt(self, draft_id: str) -> SubmissionAttempt | None:
        ...

    async def list_expired(self, before_utc: datetime) -> Sequence[Draft]:
        ...

    async def delete(self, draft_id: str) -> bool:
        ...


class UserRepositoryPort(Protocol):
    """Port for atomic user Jira credential storage."""

    async def get_credential(self, telegram_user_id: int) -> str | None:
        ...

    async def store_credential(self, telegram_user_id: int, pat: str) -> None:
        ...

    async def remove_credential(self, telegram_user_id: int) -> bool:
        ...

    async def has_credential(self, telegram_user_id: int) -> bool:
        ...


class RulesRepositoryPort(Protocol):
    """Port for reading and atomic updating of Jira classification rules."""

    async def get_rules(self) -> str:
        ...

    async def update_rules(self, new_rules_text: str) -> None:
        ...


class AIAnalyzerPort(Protocol):
    """Port for structured AI analysis of forwarded messages."""

    async def analyze_messages(
        self,
        messages: Sequence[SourceMessageRef],
        rules_text: str,
        default_project_key: str,
    ) -> JiraTaskTemplate:
        ...


class JiraGatewayPort(Protocol):
    """Port for Jira REST API operations."""

    async def test_credential(self, pat: str) -> bool:
        ...

    async def create_issue(
        self,
        template: JiraTaskTemplate,
        pat: str,
        idempotency_key: str | None = None,
    ) -> PublishedIssue:
        ...

    async def update_issue(
        self,
        issue_key: str,
        template: JiraTaskTemplate,
        pat: str,
    ) -> None:
        ...

    async def upload_attachment(
        self,
        issue_key: str,
        filename: str,
        content: bytes,
        mime_type: str,
        pat: str,
    ) -> str:
        ...

    async def get_issue_details(self, issue_key: str, pat: str) -> JiraIssueView:
        ...

    async def search_jql(self, pat: str, jql: str, max_results: int = 7) -> JiraSearchResult:
        ...

    async def get_transitions(self, issue_key: str, pat: str) -> tuple[JiraTransitionView, ...]:
        ...

    async def execute_transition(
        self, issue_key: str, transition_id: str, pat: str, comment: str | None = None
    ) -> None:
        ...

    async def add_comment(self, issue_key: str, body: str, pat: str) -> str:
        ...

    async def assign_issue(self, issue_key: str, assignee: str, pat: str) -> None:
        ...

    async def block_issue(
        self, issue_key: str, blocker_key: str, pat: str, reason: str | None = None
    ) -> None:
        ...

    async def create_generic_issue_link(
        self, pat: str, inward_key: str, outward_key: str, link_type: str = "Relates"
    ) -> None:
        ...

    async def watch_issue(self, issue_key: str, pat: str) -> None:
        ...

    async def unwatch_issue(self, issue_key: str, pat: str) -> None:
        ...

    async def unblock_issue(self, issue_key: str, link_id: str, pat: str) -> None:
        ...


class VpnManagerPort(Protocol):
    """Port for NetworkManager L2TP/IPsec VPN connection management."""

    async def is_connected(self) -> bool:
        ...

    async def ensure_connected(self) -> bool:
        ...


class TaskSchedulerPort(Protocol):
    """Port for scheduling cancellable async background tasks."""

    def schedule_timer(
        self,
        job_id: str,
        delay_seconds: float,
        callback: Any,
    ) -> None:
        ...

    def cancel_timer(self, job_id: str) -> bool:
        ...


class RendererPort(Protocol):
    """Port for rendering domain objects into user-facing presentation formats."""

    def render_preview_text(self, draft: Draft) -> str:
        ...

    def render_success_text(self, draft: Draft, published_issue: PublishedIssue) -> str:
        ...

    def render_failure_text(self, draft: Draft, error_message: str, can_retry: bool) -> str:
        ...
