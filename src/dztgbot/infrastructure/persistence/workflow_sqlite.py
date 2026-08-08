"""Transactional SQLite persistence for durable DZTGBot workflows.

The repository opens short-lived connections for each operation.  All SQLite
work runs in worker threads, and write transactions contain only synchronous
database work—never Telegram, Gemini, Jira, VPN, or file-download awaits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import TypeVar

from ...domain.callbacks import CallbackAction, CallbackTokenRecord
from ...domain.errors import (
    DomainError,
    RevisionConflictError,
    StateConflictError,
)
from ...domain.fsm import (
    EXPIRABLE_STATES,
    DraftState,
    TransitionCommand,
    evaluate_transition,
    validate_transition,
)
from ...domain.models import (
    Attachment,
    Draft,
    JiraTaskTemplate,
    MediaKind,
    PublishedIssue,
    SourceMessageRef,
    SubmissionAttempt,
)


LATEST_SCHEMA_VERSION = 4
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
MAX_BUSY_TIMEOUT_SECONDS = 30.0
DEFAULT_BATCH_LIMIT = 100

_MIGRATIONS = (
    (1, "001_initial.sql"),
    (2, "002_indexes.sql"),
    (3, "003_card_tracker.sql"),
    (4, "004_notifications.sql"),
)

_SYNCED_PATH_MARKERS = frozenset(
    {
        "dropbox",
        "google drive",
        "googledrive",
        "icloud drive",
        "icloudrive",
    }
)

_NETWORK_FILESYSTEM_TYPES = frozenset(
    {
        "9p",
        "afs",
        "cifs",
        "davfs",
        "davfs2",
        "fuse.sshfs",
        "gcsfuse",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
    }
)

_ATTEMPT_STATUSES = frozenset({"pending", "success", "failed", "unknown"})
_ATTEMPT_TRANSITIONS = {
    "pending": frozenset({"success", "failed", "unknown"}),
    "unknown": frozenset({"success", "failed"}),
    "success": frozenset(),
    "failed": frozenset(),
}

_ATTEMPT_CLAIM_STATES = frozenset(
    {
        DraftState.SUBMITTING,
        DraftState.UPDATING,
        DraftState.ATTACHING,
    }
)

_DELETABLE_RETENTION_STATES = frozenset(
    {
        DraftState.CANCELLED,
        DraftState.EXPIRED,
        DraftState.COMPLETE,
    }
)

_SAFE_CODE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789_.-"
)


class WorkflowRepositoryError(RuntimeError):
    """Base persistence error with a fixed, non-sensitive message."""


class WorkflowNotFoundError(WorkflowRepositoryError):
    """Raised when an operation targets a workflow that does not exist."""


class WorkflowDataError(WorkflowRepositoryError):
    """Raised when durable data cannot be reconstructed safely."""


class MigrationError(WorkflowRepositoryError):
    """Raised when schema history is missing, unexpected, or changed."""


class AttemptClaimConflictError(WorkflowRepositoryError):
    """Raised when another worker already owns an active attempt."""


class CallbackRecordConflictError(WorkflowRepositoryError):
    """Raised when a callback record conflicts with a rendered action."""


class AttachmentStatus(StrEnum):
    """Durable transfer states for a single attachment."""

    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    SKIPPED = "skipped"


_ATTACHMENT_TRANSITIONS = {
    AttachmentStatus.PENDING: frozenset(
        {AttachmentStatus.UPLOADING, AttachmentStatus.SKIPPED}
    ),
    AttachmentStatus.UPLOADING: frozenset(
        {AttachmentStatus.UPLOADED, AttachmentStatus.FAILED}
    ),
    AttachmentStatus.FAILED: frozenset(
        {AttachmentStatus.UPLOADING, AttachmentStatus.SKIPPED}
    ),
    AttachmentStatus.UPLOADED: frozenset(),
    AttachmentStatus.SKIPPED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    """Attachment plus repository transfer metadata."""

    position: int
    attachment: Attachment
    status: AttachmentStatus
    last_error_code: str | None
    updated_at: datetime


T = TypeVar("T")


def _is_synced_path(path: Path) -> bool:
    for part in path.parts:
        normalized = part.casefold()
        if normalized.startswith("onedrive") or normalized in _SYNCED_PATH_MARKERS:
            return True
    return False


def _unescape_proc_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _posix_mount_type(path: Path) -> str | None:
    mounts_path = Path("/proc/self/mounts")
    if not mounts_path.is_file():
        return None

    resolved = str(path.resolve(strict=False))
    best_match = ""
    best_type: str | None = None
    try:
        lines = mounts_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = _unescape_proc_mount(fields[1]).rstrip("/") or "/"
        if resolved == mount_point or resolved.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > len(best_match):
                best_match = mount_point
                best_type = fields[2].casefold()
    return best_type


def database_path_is_local(path: Path) -> bool:
    """Best-effort fail-closed check for the supported local runtime path."""

    if not path.is_absolute() or _is_synced_path(path):
        return False

    resolved = path.resolve(strict=False)
    if os.name == "nt":
        if str(resolved).startswith("\\\\"):
            return False
        try:
            import ctypes

            drive_type = ctypes.windll.kernel32.GetDriveTypeW(resolved.anchor)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
        # DRIVE_FIXED=3, DRIVE_RAMDISK=6. Removable/network/unknown are refused.
        return drive_type in {3, 6}

    mount_type = _posix_mount_type(resolved)
    return mount_type not in _NETWORK_FILESYSTEM_TYPES


def _to_db_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _from_db_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise WorkflowDataError("workflow_data_invalid")
    return parsed.astimezone(timezone.utc)


def _validate_datetime(value: datetime, *, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")


def _validate_safe_code(value: str | None, *, name: str) -> None:
    if value is None:
        return
    if not value or len(value) > 128 or any(char not in _SAFE_CODE_CHARS for char in value):
        raise ValueError(f"{name} must be a safe lowercase code")


def _template_to_json(template: JiraTaskTemplate | None) -> str | None:
    if template is None:
        return None
    return json.dumps(
        {
            "project_key": template.project_key,
            "issue_type": template.issue_type,
            "summary": template.summary,
            "description": template.description,
            "priority": template.priority,
            "labels": list(template.labels),
            "components": list(template.components),
            "assignee": template.assignee,
            "acceptance_criteria": list(template.acceptance_criteria),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _template_from_json(value: str | None) -> JiraTaskTemplate | None:
    if value is None:
        return None
    try:
        data = json.loads(value)
        if not isinstance(data, dict):
            raise TypeError
        return JiraTaskTemplate(
            project_key=data["project_key"],
            issue_type=data["issue_type"],
            summary=data["summary"],
            description=data["description"],
            priority=data["priority"],
            labels=tuple(data["labels"]),
            components=tuple(data["components"]),
            assignee=data["assignee"],
            acceptance_criteria=list(data["acceptance_criteria"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WorkflowDataError("workflow_template_invalid") from error


class SQLiteWorkflowRepository:
    """Single-host durable repository implementing the workflow domain ports."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
        enable_wal: bool = True,
    ) -> None:
        if not path.is_absolute():
            raise ValueError("workflow database path must be absolute")
        if not 0.05 <= busy_timeout_seconds <= MAX_BUSY_TIMEOUT_SECONDS:
            raise ValueError("busy timeout must be between 0.05 and 30 seconds")
        self._path = path
        self._busy_timeout_ms = int(busy_timeout_seconds * 1000)
        self._enable_wal = enable_wal
        self._initialized = False

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        """Validate the local path, apply migrations, and configure journaling."""

        try:
            await asyncio.to_thread(self._initialize_sync)
        except WorkflowRepositoryError:
            raise
        except sqlite3.Error as error:
            raise WorkflowRepositoryError("workflow_repository_initialize_failed") from error
        self._initialized = True

    async def close(self) -> None:
        """No-op: operations use short-lived connections rather than a shared handle."""

    def _initialize_sync(self) -> None:
        self._prepare_database_path()
        connection = self._connect()
        try:
            requested_mode = "WAL" if self._enable_wal else "DELETE"
            actual_mode = connection.execute(
                f"PRAGMA journal_mode={requested_mode}"
            ).fetchone()[0]
            if str(actual_mode).casefold() != requested_mode.casefold():
                raise WorkflowRepositoryError("workflow_journal_mode_unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            self._apply_migrations(connection)
        finally:
            connection.close()

        try:
            os.chmod(self._path, 0o600)
        except OSError as error:
            raise WorkflowRepositoryError("workflow_database_permissions_failed") from error

    def _prepare_database_path(self) -> None:
        if not database_path_is_local(self._path):
            raise WorkflowRepositoryError("workflow_database_must_be_local")
        if self._path.is_symlink():
            raise WorkflowRepositoryError("workflow_database_symlink_refused")
        if self._path.exists() and not stat.S_ISREG(self._path.stat().st_mode):
            raise WorkflowRepositoryError("workflow_database_not_regular")
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _begin(connection: sqlite3.Connection, *, immediate: bool = True) -> None:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.rollback()

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        reported_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if reported_version > LATEST_SCHEMA_VERSION:
            raise MigrationError("workflow_schema_version_unknown")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY NOT NULL CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL CHECK (length(checksum) = 64),
                applied_at TEXT NOT NULL
            ) STRICT
            """
        )
        known_versions = {version for version, _ in _MIGRATIONS}
        applied_rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        unexpected = {row["version"] for row in applied_rows} - known_versions
        if unexpected:
            raise MigrationError("workflow_schema_version_unknown")

        migration_directory = Path(__file__).with_name("migrations")
        applied = {row["version"]: row for row in applied_rows}
        for version, filename in _MIGRATIONS:
            migration_path = migration_directory / filename
            try:
                migration_sql = migration_path.read_text(encoding="utf-8")
            except OSError as error:
                raise MigrationError("workflow_migration_missing") from error
            checksum = hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()

            existing = applied.get(version)
            if existing is not None:
                if existing["name"] != filename or existing["checksum"] != checksum:
                    raise MigrationError("workflow_migration_checksum_mismatch")
                continue

            try:
                connection.executescript("BEGIN IMMEDIATE;\n" + migration_sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        version,
                        filename,
                        checksum,
                        _to_db_datetime(datetime.now(timezone.utc)),
                    ),
                )
                connection.execute(f"PRAGMA user_version={version}")
                connection.commit()
            except Exception:
                self._rollback(connection)
                raise

        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        if versions != list(range(1, LATEST_SCHEMA_VERSION + 1)):
            raise MigrationError("workflow_schema_history_incomplete")
        reported_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if reported_version != LATEST_SCHEMA_VERSION:
            raise MigrationError("workflow_schema_version_mismatch")

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise WorkflowRepositoryError("workflow_repository_not_initialized")

    async def _run(self, operation: Callable[..., T], *args: object) -> T:
        self._ensure_initialized()
        try:
            return await asyncio.to_thread(operation, *args)
        except (DomainError, WorkflowRepositoryError):
            raise
        except sqlite3.Error as error:
            raise WorkflowRepositoryError("workflow_repository_operation_failed") from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkflowDataError("workflow_data_invalid") from error

    async def schema_version(self) -> int:
        return await self._run(self._schema_version_sync)

    def _schema_version_sync(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
            return int(row[0])
        finally:
            connection.close()

    async def journal_mode(self) -> str:
        return await self._run(self._journal_mode_sync)

    def _journal_mode_sync(self) -> str:
        connection = self._connect()
        try:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        finally:
            connection.close()

    async def foreign_keys_enabled(self) -> bool:
        return await self._run(self._foreign_keys_enabled_sync)

    def _foreign_keys_enabled_sync(self) -> bool:
        connection = self._connect()
        try:
            return bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        finally:
            connection.close()

    async def busy_timeout_milliseconds(self) -> int:
        return await self._run(self._busy_timeout_sync)

    def _busy_timeout_sync(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        finally:
            connection.close()

    async def save(self, draft: Draft, *, expires_at: datetime | None = None) -> None:
        if expires_at is not None:
            _validate_datetime(expires_at, name="expires_at")
        _validate_safe_code(draft.last_error, name="last_error")
        await self._run(self._save_sync, draft, expires_at)

    def _save_sync(self, draft: Draft, expires_at: datetime | None) -> None:
        connection = self._connect()
        self._begin(connection)
        try:
            existing = connection.execute(
                "SELECT state, revision, expires_at FROM workflows WHERE draft_id=?",
                (draft.draft_id,),
            ).fetchone()
            if existing is not None:
                actual_revision = int(existing["revision"])
                expected_previous_revision = draft.revision - 1
                if actual_revision != expected_previous_revision:
                    raise RevisionConflictError(
                        expected_previous_revision, actual_revision
                    )
                actual_state = DraftState(existing["state"])
                if actual_state is not draft.state:
                    validate_transition(actual_state, draft.state)

            expiry_value = (
                _to_db_datetime(expires_at)
                if expires_at is not None
                else (existing["expires_at"] if existing is not None else None)
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workflows(
                        draft_id, owner_id, chat_id, message_thread_id, state,
                        revision, template_json, created_at, updated_at, expires_at,
                        last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        draft.owner_id,
                        draft.chat_id,
                        draft.message_thread_id,
                        draft.state.value,
                        draft.revision,
                        _template_to_json(draft.template),
                        _to_db_datetime(draft.created_at),
                        _to_db_datetime(draft.updated_at),
                        expiry_value,
                        draft.last_error,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE workflows
                    SET owner_id=?, chat_id=?, message_thread_id=?, state=?,
                        revision=?, template_json=?, created_at=?, updated_at=?,
                        expires_at=?, last_error=?
                    WHERE draft_id=? AND revision=?
                    """,
                    (
                        draft.owner_id,
                        draft.chat_id,
                        draft.message_thread_id,
                        draft.state.value,
                        draft.revision,
                        _template_to_json(draft.template),
                        _to_db_datetime(draft.created_at),
                        _to_db_datetime(draft.updated_at),
                        expiry_value,
                        draft.last_error,
                        draft.draft_id,
                        draft.revision - 1,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflictError(
                        draft.revision - 1, int(existing["revision"])
                    )

            connection.execute(
                "DELETE FROM source_messages WHERE draft_id=?", (draft.draft_id,)
            )
            connection.executemany(
                """
                INSERT INTO source_messages(
                    draft_id, position, message_id, chat_id, sender_id,
                    text_content, media_kind, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        draft.draft_id,
                        position,
                        source.message_id,
                        source.chat_id,
                        source.sender_id,
                        source.text,
                        source.media_kind.value,
                        _to_db_datetime(source.received_at),
                    )
                    for position, source in enumerate(draft.source_messages)
                ],
            )

            existing_attachments = {
                row["file_unique_id"]: row
                for row in connection.execute(
                    "SELECT * FROM attachments WHERE draft_id=?",
                    (draft.draft_id,),
                ).fetchall()
            }
            connection.execute(
                "DELETE FROM attachments WHERE draft_id=?", (draft.draft_id,)
            )
            attachment_values: list[tuple[object, ...]] = []
            for position, attachment in enumerate(draft.attachments):
                existing_attachment = existing_attachments.get(
                    attachment.file_unique_id
                )
                if attachment.uploaded_attachment_id:
                    status = AttachmentStatus.UPLOADED.value
                    uploaded_attachment_id = attachment.uploaded_attachment_id
                    last_error_code = None
                    attachment_updated_at = _to_db_datetime(draft.updated_at)
                elif existing_attachment is not None:
                    status = existing_attachment["status"]
                    uploaded_attachment_id = existing_attachment[
                        "uploaded_attachment_id"
                    ]
                    last_error_code = existing_attachment["last_error_code"]
                    attachment_updated_at = existing_attachment["updated_at"]
                else:
                    status = AttachmentStatus.PENDING.value
                    uploaded_attachment_id = None
                    last_error_code = None
                    attachment_updated_at = _to_db_datetime(draft.updated_at)
                attachment_values.append(
                    (
                        draft.draft_id,
                        position,
                        attachment.file_id,
                        attachment.file_unique_id,
                        attachment.media_kind.value,
                        attachment.file_name,
                        attachment.file_size,
                        status,
                        uploaded_attachment_id,
                        last_error_code,
                        attachment_updated_at,
                    )
                )
            connection.executemany(
                """
                INSERT INTO attachments(
                    draft_id, position, file_id, file_unique_id, media_kind,
                    file_name, file_size, status, uploaded_attachment_id,
                    last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                attachment_values,
            )

            if draft.published_issue is None and existing is None:
                connection.execute(
                    "DELETE FROM published_issues WHERE draft_id=?", (draft.draft_id,)
                )
            elif draft.published_issue is not None:
                self._upsert_published_issue(
                    connection, draft.draft_id, draft.published_issue
                )
            connection.commit()
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def get_by_id(self, draft_id: str) -> Draft | None:
        return await self._run(self._get_by_id_sync, draft_id)

    def _get_by_id_sync(self, draft_id: str) -> Draft | None:
        connection = self._connect()
        self._begin(connection, immediate=False)
        try:
            draft = self._load_draft(connection, draft_id)
            connection.commit()
            return draft
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    def _load_draft(
        self, connection: sqlite3.Connection, draft_id: str
    ) -> Draft | None:
        row = connection.execute(
            "SELECT * FROM workflows WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            return None

        source_rows = connection.execute(
            "SELECT * FROM source_messages WHERE draft_id=? ORDER BY position",
            (draft_id,),
        ).fetchall()
        attachment_rows = connection.execute(
            "SELECT * FROM attachments WHERE draft_id=? ORDER BY position",
            (draft_id,),
        ).fetchall()
        published_row = connection.execute(
            "SELECT * FROM published_issues WHERE draft_id=?", (draft_id,)
        ).fetchone()

        published_issue = (
            None
            if published_row is None
            else PublishedIssue(
                issue_key=published_row["issue_key"],
                issue_id=published_row["issue_id"],
                issue_url=published_row["issue_url"],
                published_at=_from_db_datetime(published_row["published_at"]),
            )
        )
        return Draft(
            draft_id=row["draft_id"],
            owner_id=int(row["owner_id"]),
            chat_id=int(row["chat_id"]),
            message_thread_id=row["message_thread_id"],
            state=DraftState(row["state"]),
            revision=int(row["revision"]),
            template=_template_from_json(row["template_json"]),
            source_messages=tuple(
                SourceMessageRef(
                    message_id=int(source["message_id"]),
                    chat_id=int(source["chat_id"]),
                    sender_id=int(source["sender_id"]),
                    text=source["text_content"],
                    media_kind=MediaKind(source["media_kind"]),
                    received_at=_from_db_datetime(source["received_at"]),
                )
                for source in source_rows
            ),
            attachments=tuple(
                Attachment(
                    file_id=attachment["file_id"],
                    file_unique_id=attachment["file_unique_id"],
                    media_kind=MediaKind(attachment["media_kind"]),
                    file_name=attachment["file_name"],
                    file_size=attachment["file_size"],
                    uploaded_attachment_id=attachment["uploaded_attachment_id"],
                )
                for attachment in attachment_rows
            ),
            created_at=_from_db_datetime(row["created_at"]),
            updated_at=_from_db_datetime(row["updated_at"]),
            published_issue=published_issue,
            last_error=row["last_error"],
        )

    async def get_expiry(self, draft_id: str) -> datetime | None:
        return await self._run(self._get_expiry_sync, draft_id)

    def _get_expiry_sync(self, draft_id: str) -> datetime | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT expires_at FROM workflows WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("workflow_not_found")
            return (
                None
                if row["expires_at"] is None
                else _from_db_datetime(row["expires_at"])
            )
        finally:
            connection.close()

    async def compare_and_swap_state(
        self,
        draft_id: str,
        expected_revision: int,
        target_state: DraftState,
        last_error: str | None = None,
    ) -> Draft:
        _validate_safe_code(last_error, name="last_error")
        return await self._run(
            self._compare_and_swap_state_sync,
            draft_id,
            expected_revision,
            target_state,
            last_error,
        )

    def _compare_and_swap_state_sync(
        self,
        draft_id: str,
        expected_revision: int,
        target_state: DraftState,
        last_error: str | None,
    ) -> Draft:
        connection = self._connect()
        self._begin(connection)
        try:
            row = connection.execute(
                "SELECT state, revision, expires_at FROM workflows WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("workflow_not_found")
            actual_state = DraftState(row["state"])
            actual_revision = int(row["revision"])
            result = evaluate_transition(
                TransitionCommand(
                    workflow_id=draft_id,
                    expected_revision=expected_revision,
                    expected_state=actual_state,
                    target_state=target_state,
                    reason_code="repository.cas",
                ),
                actual_state=actual_state,
                actual_revision=actual_revision,
            )
            expiry_value = (
                row["expires_at"] if target_state in EXPIRABLE_STATES else None
            )
            cursor = connection.execute(
                """
                UPDATE workflows
                SET state=?, revision=?, updated_at=?, expires_at=?, last_error=?
                WHERE draft_id=? AND state=? AND revision=?
                """,
                (
                    result.current_state.value,
                    result.current_revision,
                    _to_db_datetime(datetime.now(timezone.utc)),
                    expiry_value,
                    last_error,
                    draft_id,
                    result.previous_state.value,
                    result.previous_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflictError(expected_revision, actual_revision)
            draft = self._load_draft(connection, draft_id)
            if draft is None:
                raise WorkflowNotFoundError("workflow_not_found")
            connection.commit()
            return draft
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def list_expired(
        self, before_utc: datetime, *, limit: int = DEFAULT_BATCH_LIMIT
    ) -> Sequence[Draft]:
        _validate_datetime(before_utc, name="before_utc")
        _validate_limit(limit)
        return await self._run(self._list_expired_sync, before_utc, limit)

    def _list_expired_sync(
        self, before_utc: datetime, limit: int
    ) -> Sequence[Draft]:
        states = tuple(state.value for state in EXPIRABLE_STATES)
        placeholders = ",".join("?" for _ in states)
        connection = self._connect()
        self._begin(connection, immediate=False)
        try:
            rows = connection.execute(
                f"""
                SELECT draft_id FROM workflows
                WHERE expires_at IS NOT NULL
                  AND expires_at <= ?
                  AND state IN ({placeholders})
                ORDER BY expires_at, draft_id
                LIMIT ?
                """,
                (_to_db_datetime(before_utc), *states, limit),
            ).fetchall()
            drafts = tuple(
                draft
                for row in rows
                if (draft := self._load_draft(connection, row["draft_id"]))
                is not None
            )
            connection.commit()
            return drafts
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def expire_eligible(
        self, before_utc: datetime, *, limit: int = DEFAULT_BATCH_LIMIT
    ) -> int:
        _validate_datetime(before_utc, name="before_utc")
        _validate_limit(limit)
        return await self._run(self._expire_eligible_sync, before_utc, limit)

    def _expire_eligible_sync(self, before_utc: datetime, limit: int) -> int:
        states = tuple(state.value for state in EXPIRABLE_STATES)
        placeholders = ",".join("?" for _ in states)
        connection = self._connect()
        self._begin(connection)
        try:
            rows = connection.execute(
                f"""
                SELECT draft_id, state, revision FROM workflows
                WHERE expires_at IS NOT NULL
                  AND expires_at <= ?
                  AND state IN ({placeholders})
                ORDER BY expires_at, draft_id
                LIMIT ?
                """,
                (_to_db_datetime(before_utc), *states, limit),
            ).fetchall()
            changed = 0
            now_value = _to_db_datetime(datetime.now(timezone.utc))
            for row in rows:
                state = DraftState(row["state"])
                revision = int(row["revision"])
                result = evaluate_transition(
                    TransitionCommand(
                        workflow_id=row["draft_id"],
                        expected_revision=revision,
                        expected_state=state,
                        target_state=DraftState.EXPIRED,
                        reason_code="workflow.expired",
                    ),
                    actual_state=state,
                    actual_revision=revision,
                )
                cursor = connection.execute(
                    """
                    UPDATE workflows
                    SET state=?, revision=?, updated_at=?, expires_at=NULL
                    WHERE draft_id=? AND state=? AND revision=?
                    """,
                    (
                        result.current_state.value,
                        result.current_revision,
                        now_value,
                        row["draft_id"],
                        result.previous_state.value,
                        result.previous_revision,
                    ),
                )
                changed += cursor.rowcount
            connection.commit()
            return changed
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def delete(self, draft_id: str) -> bool:
        return await self._run(self._delete_sync, draft_id)

    def _delete_sync(self, draft_id: str) -> bool:
        states = tuple(state.value for state in _DELETABLE_RETENTION_STATES)
        placeholders = ",".join("?" for _ in states)
        connection = self._connect()
        self._begin(connection)
        try:
            cursor = connection.execute(
                f"DELETE FROM workflows WHERE draft_id=? AND state IN ({placeholders})",
                (draft_id, *states),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def delete_terminal(
        self, before_utc: datetime, *, limit: int = DEFAULT_BATCH_LIMIT
    ) -> int:
        _validate_datetime(before_utc, name="before_utc")
        _validate_limit(limit)
        return await self._run(self._delete_terminal_sync, before_utc, limit)

    def _delete_terminal_sync(self, before_utc: datetime, limit: int) -> int:
        states = tuple(state.value for state in _DELETABLE_RETENTION_STATES)
        placeholders = ",".join("?" for _ in states)
        connection = self._connect()
        self._begin(connection)
        try:
            rows = connection.execute(
                f"""
                SELECT draft_id FROM workflows
                WHERE updated_at <= ? AND state IN ({placeholders})
                ORDER BY updated_at, draft_id
                LIMIT ?
                """,
                (_to_db_datetime(before_utc), *states, limit),
            ).fetchall()
            ids = [(row["draft_id"],) for row in rows]
            connection.executemany(
                "DELETE FROM workflows WHERE draft_id=?", ids
            )
            connection.commit()
            return len(ids)
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def store_callback(self, record: CallbackTokenRecord) -> None:
        await self._run(self._store_callback_sync, record)

    def _store_callback_sync(self, record: CallbackTokenRecord) -> None:
        expected_state = DraftState(record.expected_state)
        connection = self._connect()
        self._begin(connection)
        try:
            workflow = connection.execute(
                """
                SELECT owner_id, chat_id, message_thread_id, state, revision
                FROM workflows WHERE draft_id=?
                """,
                (record.draft_id,),
            ).fetchone()
            if workflow is None:
                raise WorkflowNotFoundError("workflow_not_found")
            if (
                int(workflow["owner_id"]) != record.owner_user_id
                or int(workflow["chat_id"]) != record.chat_id
                or workflow["message_thread_id"] != record.message_thread_id
                or int(workflow["revision"]) != record.expected_revision
                or DraftState(workflow["state"]) is not expected_state
            ):
                raise CallbackRecordConflictError("callback_binding_conflict")
            try:
                connection.execute(
                    """
                    INSERT INTO callback_tokens(
                        token_hash, draft_id, owner_user_id, chat_id,
                        message_thread_id, preview_message_id, expected_revision,
                        expected_state, action, expires_at, one_shot, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.token_hash,
                        record.draft_id,
                        record.owner_user_id,
                        record.chat_id,
                        record.message_thread_id,
                        record.preview_message_id,
                        record.expected_revision,
                        expected_state.value,
                        record.action.value,
                        _to_db_datetime(record.expires_at),
                        int(record.one_shot),
                        (
                            None
                            if record.consumed_at is None
                            else _to_db_datetime(record.consumed_at)
                        ),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CallbackRecordConflictError("callback_record_conflict") from error
            connection.commit()
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def get_callback(self, token_hash: str) -> CallbackTokenRecord | None:
        return await self._run(self._get_callback_sync, token_hash)

    def _get_callback_sync(self, token_hash: str) -> CallbackTokenRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM callback_tokens WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                return None
            return CallbackTokenRecord(
                token_hash=row["token_hash"],
                draft_id=row["draft_id"],
                owner_user_id=int(row["owner_user_id"]),
                chat_id=int(row["chat_id"]),
                message_thread_id=row["message_thread_id"],
                preview_message_id=row["preview_message_id"],
                expected_revision=int(row["expected_revision"]),
                expected_state=row["expected_state"],
                action=CallbackAction(row["action"]),
                expires_at=_from_db_datetime(row["expires_at"]),
                one_shot=bool(row["one_shot"]),
                consumed_at=(
                    None
                    if row["consumed_at"] is None
                    else _from_db_datetime(row["consumed_at"])
                ),
            )
        finally:
            connection.close()

    async def consume_callback(self, token_hash: str, consumed_at: datetime) -> bool:
        _validate_datetime(consumed_at, name="consumed_at")
        return await self._run(
            self._consume_callback_sync, token_hash, consumed_at
        )

    def _consume_callback_sync(
        self, token_hash: str, consumed_at: datetime
    ) -> bool:
        connection = self._connect()
        self._begin(connection)
        try:
            consumed_value = _to_db_datetime(consumed_at)
            cursor = connection.execute(
                """
                UPDATE callback_tokens SET consumed_at=?
                WHERE token_hash=? AND one_shot=1
                  AND consumed_at IS NULL AND expires_at>?
                """,
                (consumed_value, token_hash, consumed_value),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def invalidate_draft_preview_tokens(
        self, draft_id: str, *, at: datetime
    ) -> int:
        """Atomically expire every still-active callback bound to one draft."""

        if not draft_id:
            raise ValueError("draft_id must not be empty")
        _validate_datetime(at, name="at")
        return await self._run(
            self._invalidate_draft_preview_tokens_sync, draft_id, at
        )

    def _invalidate_draft_preview_tokens_sync(
        self, draft_id: str, at: datetime
    ) -> int:
        connection = self._connect()
        self._begin(connection)
        try:
            invalidated_at = _to_db_datetime(at)
            cursor = connection.execute(
                """
                UPDATE callback_tokens SET expires_at=?
                WHERE draft_id=? AND consumed_at IS NULL AND expires_at>?
                """,
                (invalidated_at, draft_id, invalidated_at),
            )
            connection.commit()
            return cursor.rowcount
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def delete_expired_callbacks(
        self, before_utc: datetime, *, limit: int = DEFAULT_BATCH_LIMIT
    ) -> int:
        _validate_datetime(before_utc, name="before_utc")
        _validate_limit(limit)
        return await self._run(
            self._delete_expired_callbacks_sync, before_utc, limit
        )

    def _delete_expired_callbacks_sync(
        self, before_utc: datetime, limit: int
    ) -> int:
        connection = self._connect()
        self._begin(connection)
        try:
            rows = connection.execute(
                """
                SELECT token_hash FROM callback_tokens
                WHERE expires_at <= ? ORDER BY expires_at LIMIT ?
                """,
                (_to_db_datetime(before_utc), limit),
            ).fetchall()
            hashes = [(row["token_hash"],) for row in rows]
            connection.executemany(
                "DELETE FROM callback_tokens WHERE token_hash=?", hashes
            )
            connection.commit()
            return len(hashes)
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def claim_attempt(self, attempt: SubmissionAttempt) -> bool:
        return await self._run(self._claim_attempt_sync, attempt)

    def _claim_attempt_sync(self, attempt: SubmissionAttempt) -> bool:
        if attempt.status != "pending" or attempt.completed_at is not None:
            raise ValueError("a claimed attempt must be pending and incomplete")
        connection = self._connect()
        self._begin(connection)
        try:
            row = connection.execute(
                "SELECT state FROM workflows WHERE draft_id=?", (attempt.draft_id,)
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("workflow_not_found")
            if DraftState(row["state"]) not in _ATTEMPT_CLAIM_STATES:
                connection.rollback()
                return False
            try:
                connection.execute(
                    """
                    INSERT INTO submission_attempts(
                        attempt_id, draft_id, request_hash, attempt_number,
                        started_at, completed_at, status, error_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._attempt_values(attempt),
                )
            except sqlite3.IntegrityError:
                active_claim = connection.execute(
                    """
                    SELECT 1 FROM submission_attempts
                    WHERE draft_id=? AND status='pending'
                    """,
                    (attempt.draft_id,),
                ).fetchone()
                connection.rollback()
                if active_claim is not None:
                    return False
                raise
            connection.commit()
            return True
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def record_attempt(self, attempt: SubmissionAttempt) -> None:
        if not await self.claim_attempt(attempt):
            raise AttemptClaimConflictError("attempt_claim_conflict")

    async def update_attempt(self, attempt: SubmissionAttempt) -> None:
        await self._run(self._update_attempt_sync, attempt)

    def _update_attempt_sync(self, attempt: SubmissionAttempt) -> None:
        if attempt.status not in _ATTEMPT_STATUSES:
            raise ValueError("attempt status is invalid")
        if attempt.status != "pending" and attempt.completed_at is None:
            raise ValueError("completed attempt requires completed_at")
        _validate_safe_code(attempt.error_summary, name="error_summary")
        connection = self._connect()
        self._begin(connection)
        try:
            row = connection.execute(
                """
                SELECT draft_id, request_hash, attempt_number, started_at, status
                FROM submission_attempts
                WHERE attempt_id=?
                """,
                (attempt.attempt_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("attempt_not_found")
            if row["draft_id"] != attempt.draft_id:
                raise WorkflowDataError("attempt_draft_mismatch")
            if (
                row["request_hash"] != attempt.request_hash
                or int(row["attempt_number"]) != attempt.attempt_number
                or row["started_at"] != _to_db_datetime(attempt.started_at)
            ):
                raise WorkflowDataError("attempt_identity_mismatch")
            current_status = row["status"]
            if (
                attempt.status != current_status
                and attempt.status not in _ATTEMPT_TRANSITIONS[current_status]
            ):
                raise WorkflowDataError("attempt_status_transition_invalid")
            connection.execute(
                """
                UPDATE submission_attempts
                SET completed_at=?, status=?, error_summary=?
                WHERE attempt_id=? AND draft_id=?
                """,
                (
                    (
                        None
                        if attempt.completed_at is None
                        else _to_db_datetime(attempt.completed_at)
                    ),
                    attempt.status,
                    attempt.error_summary,
                    attempt.attempt_id,
                    attempt.draft_id,
                ),
            )
            connection.commit()
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    @staticmethod
    def _attempt_values(attempt: SubmissionAttempt) -> tuple[object, ...]:
        if attempt.status not in _ATTEMPT_STATUSES:
            raise ValueError("attempt status is invalid")
        _validate_safe_code(attempt.error_summary, name="error_summary")
        return (
            attempt.attempt_id,
            attempt.draft_id,
            attempt.request_hash,
            attempt.attempt_number,
            _to_db_datetime(attempt.started_at),
            (
                None
                if attempt.completed_at is None
                else _to_db_datetime(attempt.completed_at)
            ),
            attempt.status,
            attempt.error_summary,
        )

    async def get_latest_attempt(
        self, draft_id: str
    ) -> SubmissionAttempt | None:
        return await self._run(self._get_latest_attempt_sync, draft_id)

    def _get_latest_attempt_sync(
        self, draft_id: str
    ) -> SubmissionAttempt | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM submission_attempts
                WHERE draft_id=? ORDER BY attempt_number DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
            if row is None:
                return None
            return SubmissionAttempt(
                attempt_id=row["attempt_id"],
                draft_id=row["draft_id"],
                request_hash=row["request_hash"],
                attempt_number=int(row["attempt_number"]),
                started_at=_from_db_datetime(row["started_at"]),
                completed_at=(
                    None
                    if row["completed_at"] is None
                    else _from_db_datetime(row["completed_at"])
                ),
                status=row["status"],
                error_summary=row["error_summary"],
            )
        finally:
            connection.close()

    async def list_attachments(self, draft_id: str) -> Sequence[StoredAttachment]:
        return await self._run(self._list_attachments_sync, draft_id)

    def _list_attachments_sync(self, draft_id: str) -> Sequence[StoredAttachment]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM attachments WHERE draft_id=? ORDER BY position",
                (draft_id,),
            ).fetchall()
            return tuple(
                StoredAttachment(
                    position=int(row["position"]),
                    attachment=Attachment(
                        file_id=row["file_id"],
                        file_unique_id=row["file_unique_id"],
                        media_kind=MediaKind(row["media_kind"]),
                        file_name=row["file_name"],
                        file_size=row["file_size"],
                        uploaded_attachment_id=row["uploaded_attachment_id"],
                    ),
                    status=AttachmentStatus(row["status"]),
                    last_error_code=row["last_error_code"],
                    updated_at=_from_db_datetime(row["updated_at"]),
                )
                for row in rows
            )
        finally:
            connection.close()

    async def set_attachment_status(
        self,
        draft_id: str,
        file_unique_id: str,
        *,
        expected_status: AttachmentStatus,
        target_status: AttachmentStatus,
        updated_at: datetime,
        uploaded_attachment_id: str | None = None,
        last_error_code: str | None = None,
    ) -> bool:
        _validate_datetime(updated_at, name="updated_at")
        _validate_safe_code(last_error_code, name="last_error_code")
        if target_status not in _ATTACHMENT_TRANSITIONS[expected_status]:
            raise ValueError("attachment status transition is invalid")
        if target_status is AttachmentStatus.UPLOADED and not uploaded_attachment_id:
            raise ValueError("uploaded attachment requires its Jira attachment id")
        return await self._run(
            self._set_attachment_status_sync,
            draft_id,
            file_unique_id,
            expected_status,
            target_status,
            updated_at,
            uploaded_attachment_id,
            last_error_code,
        )

    def _set_attachment_status_sync(
        self,
        draft_id: str,
        file_unique_id: str,
        expected_status: AttachmentStatus,
        target_status: AttachmentStatus,
        updated_at: datetime,
        uploaded_attachment_id: str | None,
        last_error_code: str | None,
    ) -> bool:
        connection = self._connect()
        self._begin(connection)
        try:
            cursor = connection.execute(
                """
                UPDATE attachments
                SET status=?, uploaded_attachment_id=?, last_error_code=?, updated_at=?
                WHERE draft_id=? AND file_unique_id=? AND status=?
                """,
                (
                    target_status.value,
                    uploaded_attachment_id,
                    last_error_code,
                    _to_db_datetime(updated_at),
                    draft_id,
                    file_unique_id,
                    expected_status.value,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def store_published_issue(
        self, draft_id: str, issue: PublishedIssue
    ) -> None:
        await self._run(self._store_published_issue_sync, draft_id, issue)

    def _store_published_issue_sync(
        self, draft_id: str, issue: PublishedIssue
    ) -> None:
        connection = self._connect()
        self._begin(connection)
        try:
            if connection.execute(
                "SELECT 1 FROM workflows WHERE draft_id=?", (draft_id,)
            ).fetchone() is None:
                raise WorkflowNotFoundError("workflow_not_found")
            self._upsert_published_issue(connection, draft_id, issue)
            connection.commit()
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    @staticmethod
    def _upsert_published_issue(
        connection: sqlite3.Connection,
        draft_id: str,
        issue: PublishedIssue,
    ) -> None:
        connection.execute(
            """
            INSERT INTO published_issues(
                draft_id, issue_key, issue_id, issue_url, published_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
                issue_key=excluded.issue_key,
                issue_id=excluded.issue_id,
                issue_url=excluded.issue_url,
                published_at=excluded.published_at
            """,
            (
                draft_id,
                issue.issue_key,
                issue.issue_id,
                issue.issue_url,
                _to_db_datetime(issue.published_at),
            ),
        )

    async def get_published_issue(self, draft_id: str) -> PublishedIssue | None:
        return await self._run(self._get_published_issue_sync, draft_id)

    def _get_published_issue_sync(self, draft_id: str) -> PublishedIssue | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM published_issues WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None:
                return None
            return PublishedIssue(
                issue_key=row["issue_key"],
                issue_id=row["issue_id"],
                issue_url=row["issue_url"],
                published_at=_from_db_datetime(row["published_at"]),
            )
        finally:
            connection.close()

    async def register_card_message(
        self, chat_id: int, message_id: int, issue_key: str, owner_id: int
    ) -> None:
        await self._run(
            self._register_card_message_sync, chat_id, message_id, issue_key, owner_id
        )

    def _register_card_message_sync(
        self, chat_id: int, message_id: int, issue_key: str, owner_id: int
    ) -> None:
        connection = self._connect()
        self._begin(connection)
        try:
            now_str = _to_db_datetime(datetime.now(timezone.utc))
            connection.execute(
                """
                INSERT INTO card_message_tracker(chat_id, message_id, issue_key, owner_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    issue_key=excluded.issue_key,
                    owner_id=excluded.owner_id,
                    updated_at=excluded.updated_at
                """,
                (chat_id, message_id, issue_key, owner_id, now_str),
            )
            connection.commit()
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    async def get_card_messages_for_issue(
        self, issue_key: str
    ) -> tuple[tuple[int, int, int], ...]:
        return await self._run(self._get_card_messages_for_issue_sync, issue_key)

    def _get_card_messages_for_issue_sync(
        self, issue_key: str
    ) -> tuple[tuple[int, int, int], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT chat_id, message_id, owner_id FROM card_message_tracker WHERE issue_key=? ORDER BY updated_at DESC",
                (issue_key,),
            ).fetchall()
            return tuple((int(r["chat_id"]), int(r["message_id"]), int(r["owner_id"])) for r in rows)
        finally:
            connection.close()

    async def get_last_notified_update(self, user_id: int, issue_key: str) -> str | None:
        return await self._run(self._get_last_notified_update_sync, user_id, issue_key)

    def _get_last_notified_update_sync(self, user_id: int, issue_key: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT last_updated FROM user_notification_tracker WHERE user_id=? AND issue_key=?",
                (user_id, issue_key),
            ).fetchone()
            return str(row["last_updated"]) if row is not None else None
        finally:
            connection.close()

    async def record_notification(self, user_id: int, issue_key: str, last_updated: str) -> None:
        await self._run(self._record_notification_sync, user_id, issue_key, last_updated)

    def _record_notification_sync(self, user_id: int, issue_key: str, last_updated: str) -> None:
        connection = self._connect()
        self._begin(connection)
        try:
            connection.execute(
                """
                INSERT INTO user_notification_tracker(user_id, issue_key, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, issue_key) DO UPDATE SET last_updated=excluded.last_updated
                """,
                (user_id, issue_key, last_updated),
            )
            connection.commit()
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()

