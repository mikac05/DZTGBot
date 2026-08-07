"""Event-driven, workflow-scoped intake and analysis orchestration.

The service contains no Telegram or provider SDK types. Incoming messages are
validated and collected under a short in-memory lock. A scheduler-owned,
cancellable deadline detaches each batch before any repository, rules, AI, or
presentation await occurs.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import logging

from dztgbot.domain.errors import DomainError
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Attachment, Draft, MediaKind, SourceMessageRef
from dztgbot.domain.ports import (
    AIAnalyzerPort,
    ClockPort,
    DraftRepositoryPort,
    IdGeneratorPort,
    RulesRepositoryPort,
    TaskSchedulerPort,
)


LOGGER = logging.getLogger(__name__)

MAX_BATCH_SIZE = 20
MAX_PROMPT_CHARACTERS = 32_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
DEFAULT_BATCH_WINDOW_SECONDS = 2.5
DEFAULT_RECENT_MESSAGE_LIMIT = 2_048


class IntakeValidationError(ValueError):
    """A fixed-code rejection that never embeds message or attachment content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DuplicateMessageError(IntakeValidationError):
    def __init__(self) -> None:
        super().__init__("duplicate_message")


class DuplicateAttachmentError(IntakeValidationError):
    def __init__(self) -> None:
        super().__init__("duplicate_attachment")


class BatchLimitExceededError(IntakeValidationError):
    def __init__(self) -> None:
        super().__init__("batch_message_limit")


class PromptBudgetExceededError(IntakeValidationError):
    def __init__(self) -> None:
        super().__init__("prompt_character_budget")


class AttachmentEligibilityError(IntakeValidationError):
    def __init__(self, code: str = "attachment_ineligible") -> None:
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class IntakeScope:
    """Identity boundary for one independently collected Telegram batch."""

    owner_id: int
    chat_id: int
    message_thread_id: int | None = None

    def __post_init__(self) -> None:
        if self.owner_id <= 0:
            raise ValueError("owner_id must be positive")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if self.message_thread_id is not None and self.message_thread_id <= 0:
            raise ValueError("message_thread_id must be positive when present")


@dataclass(frozen=True, slots=True)
class CollectionReceipt:
    """Result of accepting one message into a pending batch."""

    draft_id: str
    batch_size: int
    prompt_characters: int
    deadline_job_id: str


@dataclass(slots=True)
class _PendingBatch:
    draft_id: str
    messages: list[SourceMessageRef] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    message_keys: set[tuple[int, int]] = field(default_factory=set)
    attachment_ids: set[str] = field(default_factory=set)
    prompt_characters: int = 0
    deadline_generation: int = 0
    deadline_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SealedBatch:
    scope: IntakeScope
    draft_id: str
    messages: tuple[SourceMessageRef, ...]
    attachments: tuple[Attachment, ...]


DraftObserver = Callable[[Draft], Awaitable[None]]


class IntakeService:
    """Collect messages by owner/chat/thread and persist isolated draft batches."""

    def __init__(
        self,
        *,
        repository: DraftRepositoryPort,
        analyzer: AIAnalyzerPort,
        rules_repository: RulesRepositoryPort,
        scheduler: TaskSchedulerPort,
        clock: ClockPort,
        id_generator: IdGeneratorPort,
        default_project_key: str,
        batch_window_seconds: float = DEFAULT_BATCH_WINDOW_SECONDS,
        max_batch_size: int = MAX_BATCH_SIZE,
        max_prompt_characters: int = MAX_PROMPT_CHARACTERS,
        max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
        recent_message_limit: int = DEFAULT_RECENT_MESSAGE_LIMIT,
        on_draft_ready: DraftObserver | None = None,
        on_draft_failed: DraftObserver | None = None,
    ) -> None:
        if not default_project_key.strip():
            raise ValueError("default_project_key must not be empty")
        if batch_window_seconds <= 0:
            raise ValueError("batch_window_seconds must be positive")
        if max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive")
        if max_prompt_characters <= 0:
            raise ValueError("max_prompt_characters must be positive")
        if max_attachment_bytes <= 0:
            raise ValueError("max_attachment_bytes must be positive")
        if recent_message_limit < max_batch_size:
            raise ValueError("recent_message_limit must cover at least one batch")

        self._repository = repository
        self._analyzer = analyzer
        self._rules_repository = rules_repository
        self._scheduler = scheduler
        self._clock = clock
        self._id_generator = id_generator
        self._default_project_key = default_project_key
        self._batch_window_seconds = batch_window_seconds
        self._max_batch_size = max_batch_size
        self._max_prompt_characters = max_prompt_characters
        self._max_attachment_bytes = max_attachment_bytes
        self._recent_message_limit = recent_message_limit
        self._on_draft_ready = on_draft_ready
        self._on_draft_failed = on_draft_failed

        self._collection_lock = asyncio.Lock()
        self._pending: dict[IntakeScope, _PendingBatch] = {}
        self._recent_messages: OrderedDict[
            tuple[IntakeScope, int, int], None
        ] = OrderedDict()

    async def collect_message(
        self,
        *,
        owner_id: int,
        chat_id: int,
        message_thread_id: int | None,
        message: SourceMessageRef,
        attachment: Attachment | None = None,
    ) -> CollectionReceipt:
        """Validate and append one message, then reset its scope deadline.

        Validation and mutation are performed atomically under a short lock.
        The scheduler calls back later; no repository or external I/O occurs
        while this lock is held.
        """

        scope = IntakeScope(owner_id, chat_id, message_thread_id)
        self._validate_message_binding(scope, message)

        async with self._collection_lock:
            batch = self._pending.get(scope)
            message_key = (message.chat_id, message.message_id)
            recent_key = (scope, *message_key)
            if recent_key in self._recent_messages:
                raise DuplicateMessageError()

            current_count = 0 if batch is None else len(batch.messages)
            if current_count >= self._max_batch_size:
                raise BatchLimitExceededError()

            message_characters = len(message.text)
            current_characters = 0 if batch is None else batch.prompt_characters
            if current_characters + message_characters > self._max_prompt_characters:
                raise PromptBudgetExceededError()

            self._validate_attachment(message, attachment, batch)

            if batch is None:
                batch = _PendingBatch(draft_id=self._id_generator.generate_uuid())
                self._pending[scope] = batch
            batch.messages.append(message)
            batch.message_keys.add(message_key)
            batch.prompt_characters += message_characters
            if attachment is not None:
                batch.attachments.append(attachment)
                batch.attachment_ids.add(attachment.file_unique_id)

            self._remember_message_locked(scope, message_key)
            deadline_job_id = self._reschedule_deadline_locked(scope, batch)
            return CollectionReceipt(
                draft_id=batch.draft_id,
                batch_size=len(batch.messages),
                prompt_characters=batch.prompt_characters,
                deadline_job_id=deadline_job_id,
            )

    async def pending_count(
        self,
        *,
        owner_id: int,
        chat_id: int,
        message_thread_id: int | None,
    ) -> int:
        scope = IntakeScope(owner_id, chat_id, message_thread_id)
        async with self._collection_lock:
            batch = self._pending.get(scope)
            return 0 if batch is None else len(batch.messages)

    async def cancel_pending(
        self,
        *,
        owner_id: int,
        chat_id: int,
        message_thread_id: int | None,
    ) -> bool:
        """Cancel and discard only the not-yet-sealed batch for a scope."""

        scope = IntakeScope(owner_id, chat_id, message_thread_id)
        async with self._collection_lock:
            batch = self._pending.pop(scope, None)
            if batch is None:
                return False
            if batch.deadline_job_id is not None:
                self._scheduler.cancel_timer(batch.deadline_job_id)
            return True

    async def cancel_all_pending(self) -> int:
        """Cancel all owned deadlines during application shutdown."""

        async with self._collection_lock:
            batches = tuple(self._pending.values())
            self._pending.clear()
            for batch in batches:
                if batch.deadline_job_id is not None:
                    self._scheduler.cancel_timer(batch.deadline_job_id)
            return len(batches)

    async def flush_scope(
        self,
        *,
        owner_id: int,
        chat_id: int,
        message_thread_id: int | None,
    ) -> Draft | None:
        """Seal a scope immediately, cancelling its pending deadline."""

        scope = IntakeScope(owner_id, chat_id, message_thread_id)
        async with self._collection_lock:
            sealed = self._detach_batch_locked(scope, cancel_deadline=True)
        if sealed is None:
            return None
        return await self._process_sealed_batch(sealed)

    def _validate_message_binding(
        self, scope: IntakeScope, message: SourceMessageRef
    ) -> None:
        if message.chat_id != scope.chat_id:
            raise IntakeValidationError("message_chat_mismatch")
        if not isinstance(message.text, str):
            raise IntakeValidationError("message_text_type")
        if not isinstance(message.media_kind, MediaKind):
            raise IntakeValidationError("message_media_kind")

    def _validate_attachment(
        self,
        message: SourceMessageRef,
        attachment: Attachment | None,
        batch: _PendingBatch | None,
    ) -> None:
        if message.media_kind is MediaKind.PHOTO and attachment is None:
            raise AttachmentEligibilityError("photo_attachment_missing")
        if attachment is None:
            return
        if (
            message.media_kind is not MediaKind.PHOTO
            or attachment.media_kind is not MediaKind.PHOTO
        ):
            raise AttachmentEligibilityError()
        if attachment.file_size is not None and (
            attachment.file_size < 0
            or attachment.file_size > self._max_attachment_bytes
        ):
            raise AttachmentEligibilityError("attachment_size")
        if batch is not None and attachment.file_unique_id in batch.attachment_ids:
            raise DuplicateAttachmentError()

    def _remember_message_locked(
        self, scope: IntakeScope, message_key: tuple[int, int]
    ) -> None:
        recent_key = (scope, *message_key)
        self._recent_messages[recent_key] = None
        self._recent_messages.move_to_end(recent_key)
        while len(self._recent_messages) > self._recent_message_limit:
            self._recent_messages.popitem(last=False)

    def _reschedule_deadline_locked(
        self, scope: IntakeScope, batch: _PendingBatch
    ) -> str:
        if batch.deadline_job_id is not None:
            self._scheduler.cancel_timer(batch.deadline_job_id)
        batch.deadline_generation += 1
        generation = batch.deadline_generation
        job_id = f"intake:{batch.draft_id}:{generation}"
        batch.deadline_job_id = job_id
        delay = (
            0.0
            if len(batch.messages) >= self._max_batch_size
            else self._batch_window_seconds
        )

        def deadline_callback() -> Awaitable[Draft | None]:
            return self._deadline_elapsed(
                scope=scope,
                draft_id=batch.draft_id,
                generation=generation,
                job_id=job_id,
            )

        self._scheduler.schedule_timer(job_id, delay, deadline_callback)
        return job_id

    async def _deadline_elapsed(
        self,
        *,
        scope: IntakeScope,
        draft_id: str,
        generation: int,
        job_id: str,
    ) -> Draft | None:
        async with self._collection_lock:
            batch = self._pending.get(scope)
            if (
                batch is None
                or batch.draft_id != draft_id
                or batch.deadline_generation != generation
                or batch.deadline_job_id != job_id
            ):
                return None
            sealed = self._detach_batch_locked(scope, cancel_deadline=False)
        if sealed is None:
            return None
        return await self._process_sealed_batch(sealed)

    def _detach_batch_locked(
        self, scope: IntakeScope, *, cancel_deadline: bool
    ) -> _SealedBatch | None:
        batch = self._pending.pop(scope, None)
        if batch is None:
            return None
        if cancel_deadline and batch.deadline_job_id is not None:
            self._scheduler.cancel_timer(batch.deadline_job_id)
        return _SealedBatch(
            scope=scope,
            draft_id=batch.draft_id,
            messages=tuple(batch.messages),
            attachments=tuple(batch.attachments),
        )

    async def _process_sealed_batch(self, sealed: _SealedBatch) -> Draft:
        now = self._clock.now()
        collecting = Draft(
            draft_id=sealed.draft_id,
            owner_id=sealed.scope.owner_id,
            chat_id=sealed.scope.chat_id,
            message_thread_id=sealed.scope.message_thread_id,
            state=DraftState.COLLECTING,
            revision=1,
            source_messages=sealed.messages,
            attachments=sealed.attachments,
            created_at=now,
            updated_at=now,
        )
        await self._repository.save(collecting)
        analyzing = await self._repository.compare_and_swap_state(
            collecting.draft_id,
            collecting.revision,
            DraftState.ANALYZING,
        )

        try:
            rules_text = await self._rules_repository.get_rules()
            template = await self._analyzer.analyze_messages(
                analyzing.source_messages,
                rules_text,
                self._default_project_key,
            )
        except Exception as error:
            LOGGER.warning("Intake analysis failed (%s)", type(error).__name__)
            return await self._mark_analysis_failed(analyzing)

        current = await self._repository.get_by_id(analyzing.draft_id)
        if (
            current is None
            or current.state is not DraftState.ANALYZING
            or current.revision != analyzing.revision
        ):
            return current if current is not None else analyzing

        with_template = replace(
            current,
            template=template,
            updated_at=self._clock.now(),
            last_error=None,
        )
        try:
            await self._repository.save(with_template)
            ready = await self._repository.compare_and_swap_state(
                with_template.draft_id,
                with_template.revision,
                DraftState.REVIEW,
            )
        except DomainError:
            latest = await self._repository.get_by_id(with_template.draft_id)
            return latest if latest is not None else with_template

        if self._on_draft_ready is not None:
            await self._on_draft_ready(ready)
        return ready

    async def _mark_analysis_failed(self, analyzing: Draft) -> Draft:
        try:
            failed = await self._repository.compare_and_swap_state(
                analyzing.draft_id,
                analyzing.revision,
                DraftState.ANALYSIS_FAILED,
                last_error="analysis_failed",
            )
        except DomainError:
            latest = await self._repository.get_by_id(analyzing.draft_id)
            return latest if latest is not None else analyzing
        if self._on_draft_failed is not None:
            await self._on_draft_failed(failed)
        return failed
