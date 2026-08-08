"""Bounded, durable attachment upload orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol, Sequence

from dztgbot.domain.errors import DomainError
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Attachment, Draft


class AttachmentStatus(StrEnum):
    """Structural mirror of the durable transfer states exposed by the repository."""

    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    SKIPPED = "skipped"


class StoredAttachment(Protocol):
    attachment: Attachment
    status: AttachmentStatus


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    content: bytes
    filename: str
    mime_type: str

    def __post_init__(self) -> None:
        if not self.content or not self.filename or not self.mime_type:
            raise ValueError("attachment content metadata must not be empty")


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    max_count: int = 10
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 25 * 1024 * 1024
    allowed_mime_types: frozenset[str] = frozenset(
        {"image/jpeg", "image/png", "image/webp"}
    )

    def __post_init__(self) -> None:
        if min(self.max_count, self.max_file_bytes, self.max_total_bytes) <= 0:
            raise ValueError("attachment bounds must be positive")
        if not self.allowed_mime_types:
            raise ValueError("allowed_mime_types must not be empty")


@dataclass(frozen=True, slots=True)
class AttachmentBatchResult:
    draft: Draft
    uploaded: int
    failed: int
    skipped: int


class AttachmentRepository(Protocol):
    async def get_by_id(self, draft_id: str) -> Draft | None: ...
    async def compare_and_swap_state(self, draft_id: str, expected_revision: int, target_state: DraftState, last_error: str | None = None) -> Draft: ...
    async def list_attachments(self, draft_id: str) -> Sequence[StoredAttachment]: ...
    async def set_attachment_status(self, draft_id: str, file_unique_id: str, *, expected_status: AttachmentStatus, target_status: AttachmentStatus, updated_at: datetime, uploaded_attachment_id: str | None = None, last_error_code: str | None = None) -> bool: ...


class AttachmentGateway(Protocol):
    async def upload_attachment(self, issue_key: str, filename: str, content: bytes, mime_type: str, pat: str) -> str: ...


class AttachmentLoader(Protocol):
    async def load(self, file_id: str) -> AttachmentContent: ...


class AttachmentService:
    """Uploads only to an already-created issue; it has no create capability."""

    def __init__(
        self,
        repository: AttachmentRepository,
        gateway: AttachmentGateway,
        loader: AttachmentLoader,
        *,
        policy: AttachmentPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._loader = loader
        self._policy = policy or AttachmentPolicy()

    async def upload_pending(self, draft_id: str, pat: str) -> AttachmentBatchResult:
        draft = await self._repository.get_by_id(draft_id)
        if draft is None or draft.published_issue is None:
            raise ValueError("attachment_workflow_unavailable")
        if draft.state not in {DraftState.CREATED, DraftState.ATTACHMENT_PARTIAL}:
            raise ValueError("attachment_state_conflict")
        if not draft.attachments:
            complete = await self._repository.compare_and_swap_state(
                draft_id, draft.revision, DraftState.COMPLETE
            )
            return AttachmentBatchResult(complete, 0, 0, 0)

        attaching = await self._repository.compare_and_swap_state(
            draft_id, draft.revision, DraftState.ATTACHING
        )
        records = tuple(await self._repository.list_attachments(draft_id))
        uploaded = failed = skipped = 0
        total_bytes = 0
        seen: set[str] = set()

        for index, record in enumerate(records):
            unique_id = record.attachment.file_unique_id
            if record.status in {AttachmentStatus.UPLOADED, AttachmentStatus.SKIPPED}:
                seen.add(unique_id)
                continue
            if unique_id in seen or index >= self._policy.max_count:
                if await self._skip(draft_id, record, "attachment_duplicate" if unique_id in seen else "attachment_count_limit"):
                    skipped += 1
                continue
            seen.add(unique_id)
            declared_size = record.attachment.file_size
            if declared_size is not None and (
                declared_size > self._policy.max_file_bytes
                or total_bytes + declared_size > self._policy.max_total_bytes
            ):
                if await self._skip(draft_id, record, "attachment_size_limit"):
                    skipped += 1
                continue
            claimed = await self._repository.set_attachment_status(
                draft_id,
                unique_id,
                expected_status=record.status,
                target_status=AttachmentStatus.UPLOADING,
                updated_at=self._now(),
            )
            if not claimed:
                continue
            try:
                loaded = await self._loader.load(record.attachment.file_id)
                size = len(loaded.content)
                if loaded.mime_type not in self._policy.allowed_mime_types:
                    raise _AttachmentRejected("attachment_type_limit")
                if size > self._policy.max_file_bytes or total_bytes + size > self._policy.max_total_bytes:
                    raise _AttachmentRejected("attachment_size_limit")
                attachment_id = await self._gateway.upload_attachment(
                    draft.published_issue.issue_key,
                    loaded.filename,
                    loaded.content,
                    loaded.mime_type,
                    pat,
                )
            except _AttachmentRejected as error:
                await self._repository.set_attachment_status(
                    draft_id,
                    unique_id,
                    expected_status=AttachmentStatus.UPLOADING,
                    target_status=AttachmentStatus.FAILED,
                    updated_at=self._now(),
                    last_error_code=error.code,
                )
                failed += 1
            except DomainError as error:
                await self._repository.set_attachment_status(
                    draft_id,
                    unique_id,
                    expected_status=AttachmentStatus.UPLOADING,
                    target_status=AttachmentStatus.FAILED,
                    updated_at=self._now(),
                    last_error_code=error.classification.safe_code.value,
                )
                failed += 1
            except Exception:
                await self._repository.set_attachment_status(
                    draft_id,
                    unique_id,
                    expected_status=AttachmentStatus.UPLOADING,
                    target_status=AttachmentStatus.FAILED,
                    updated_at=self._now(),
                    last_error_code="attachment_failed",
                )
                failed += 1
            else:
                await self._repository.set_attachment_status(
                    draft_id,
                    unique_id,
                    expected_status=AttachmentStatus.UPLOADING,
                    target_status=AttachmentStatus.UPLOADED,
                    updated_at=self._now(),
                    uploaded_attachment_id=attachment_id,
                )
                uploaded += 1
                total_bytes += size

        final_records = tuple(await self._repository.list_attachments(draft_id))
        partial = any(record.status == AttachmentStatus.FAILED for record in final_records)
        target = DraftState.ATTACHMENT_PARTIAL if partial else DraftState.COMPLETE
        final = await self._repository.compare_and_swap_state(
            draft_id,
            attaching.revision,
            target,
            "attachment_partial" if partial else None,
        )
        return AttachmentBatchResult(final, uploaded, failed, skipped)

    async def _skip(
        self, draft_id: str, record: StoredAttachment, code: str
    ) -> bool:
        if record.status == AttachmentStatus.UPLOADING:
            return False
        return await self._repository.set_attachment_status(
            draft_id,
            record.attachment.file_unique_id,
            expected_status=record.status,
            target_status=AttachmentStatus.SKIPPED,
            updated_at=self._now(),
            last_error_code=code,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


class _AttachmentRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


__all__ = [
    "AttachmentBatchResult", "AttachmentContent", "AttachmentLoader",
    "AttachmentPolicy", "AttachmentService",
]
