"""Callback authorization service (P3-G).

Parses bound ``j1:<action>:<token>`` callbacks, resolves hashed token records,
loads the target draft, enforces private-chat identity binding, and atomically
consumes one-shot actions. No Telegram imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol, Sequence

from dztgbot.domain.callbacks import (
    CallbackAction,
    CallbackAuthorizationInput,
    CallbackParseError,
    CallbackTokenRecord,
    ParsedCallback,
    build_token_record,
    default_one_shot,
    encode_callback_data,
    generate_opaque_token,
    hash_opaque_token,
    parse_callback_data,
)
from dztgbot.domain.models import Draft
from dztgbot.domain.policy import (
    DenialCode,
    PolicyDecision,
    authorize_callback,
    require_private_chat,
    user_message_for_denial,
)
from dztgbot.domain.ports import ClockPort, DraftRepositoryPort

# Default lifetime for preview button tokens.
DEFAULT_PREVIEW_TOKEN_TTL = timedelta(hours=1)


class CallbackTokenStorePort(Protocol):
    """Persistence port for hashed callback token records.

    Implemented by SQLite workflow storage (and test fakes). The raw opaque
    token is never stored — only its SHA-256 hash.
    """

    async def get_callback(self, token_hash: str) -> CallbackTokenRecord | None:
        ...

    async def store_callback(self, record: CallbackTokenRecord) -> None:
        ...

    async def consume_callback(self, token_hash: str, consumed_at: datetime) -> bool:
        """Atomically mark a one-shot token consumed. Return False if already used/expired."""
        ...

    async def invalidate_draft_preview_tokens(
        self, draft_id: str, *, at: datetime
    ) -> int:
        """Expire or remove all preview tokens for ``draft_id``. Return count affected."""
        ...


@dataclass(frozen=True, slots=True)
class CallbackAuthorizationResult:
    """Outcome of authorizing one inbound callback.

    On denial, ``denial_code`` is set and ``draft``/``record`` may still be
    populated when safe for diagnostics (never include raw tokens).
    """

    allowed: bool
    denial_code: DenialCode | None
    action: CallbackAction | None
    draft: Draft | None
    record: CallbackTokenRecord | None
    token_hash: str | None = None

    @property
    def user_message(self) -> str | None:
        if self.denial_code is None:
            return None
        return user_message_for_denial(self.denial_code)

    @classmethod
    def deny(
        cls,
        code: DenialCode,
        *,
        action: CallbackAction | None = None,
        draft: Draft | None = None,
        record: CallbackTokenRecord | None = None,
        token_hash: str | None = None,
    ) -> CallbackAuthorizationResult:
        return cls(
            allowed=False,
            denial_code=code,
            action=action,
            draft=draft,
            record=record,
            token_hash=token_hash,
        )

    @classmethod
    def allow(
        cls,
        *,
        action: CallbackAction,
        draft: Draft,
        record: CallbackTokenRecord,
        token_hash: str,
    ) -> CallbackAuthorizationResult:
        return cls(
            allowed=True,
            denial_code=None,
            action=action,
            draft=draft,
            record=record,
            token_hash=token_hash,
        )


@dataclass(frozen=True, slots=True)
class IssuedCallbackButton:
    """One rendered button: wire ``callback_data`` plus the stored hash."""

    action: CallbackAction
    callback_data: str
    token_hash: str
    record: CallbackTokenRecord


class CallbackService:
    """Authorize and issue bound preview callbacks without Telegram types."""

    def __init__(
        self,
        drafts: DraftRepositoryPort,
        tokens: CallbackTokenStorePort,
        clock: ClockPort | None = None,
        *,
        preview_token_ttl: timedelta = DEFAULT_PREVIEW_TOKEN_TTL,
    ) -> None:
        self._drafts = drafts
        self._tokens = tokens
        self._clock = clock
        self._preview_token_ttl = preview_token_ttl

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    async def authorize(
        self,
        *,
        raw_callback_data: str | None,
        actor_user_id: int,
        chat_id: int,
        chat_type: str,
        message_thread_id: int | None = None,
        preview_message_id: int | None = None,
    ) -> CallbackAuthorizationResult:
        """Parse, resolve, authorize, and optionally consume a callback."""

        private = require_private_chat(chat_type)
        if not private.allowed:
            return CallbackAuthorizationResult.deny(
                private.denial_code or DenialCode.NOT_PRIVATE_CHAT
            )

        try:
            parsed = parse_callback_data(raw_callback_data)
        except CallbackParseError:
            return CallbackAuthorizationResult.deny(DenialCode.MALFORMED_CALLBACK)

        try:
            token_hash = hash_opaque_token(parsed.opaque_token)
        except CallbackParseError:
            return CallbackAuthorizationResult.deny(DenialCode.MALFORMED_CALLBACK)

        record = await self._tokens.get_callback(token_hash)
        if record is None:
            return CallbackAuthorizationResult.deny(
                DenialCode.UNKNOWN_TOKEN,
                action=parsed.action,
                token_hash=token_hash,
            )

        draft = await self._drafts.get_by_id(record.draft_id)
        current_revision = draft.revision if draft is not None else None
        current_state = draft.state.value if draft is not None else None

        now = self._now()
        request = CallbackAuthorizationInput(
            actor_user_id=actor_user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            message_thread_id=message_thread_id,
            preview_message_id=preview_message_id,
            action=parsed.action,
            opaque_token=parsed.opaque_token,
            now=now,
        )
        decision = authorize_callback(
            request,
            record,
            current_revision=current_revision,
            current_state=current_state,
        )
        if not decision.allowed:
            return CallbackAuthorizationResult.deny(
                decision.denial_code or DenialCode.DENIED,
                action=parsed.action,
                draft=draft,
                record=record,
                token_hash=token_hash,
            )

        if draft is None:
            # Token pointed at a missing draft after policy hash match — treat as unknown.
            return CallbackAuthorizationResult.deny(
                DenialCode.UNKNOWN_TOKEN,
                action=parsed.action,
                record=record,
                token_hash=token_hash,
            )

        # One-shot: consume atomically so replays cannot double-mutate.
        if record.one_shot or default_one_shot(parsed.action):
            consumed = await self._tokens.consume_callback(token_hash, now)
            if not consumed:
                return CallbackAuthorizationResult.deny(
                    DenialCode.TOKEN_CONSUMED,
                    action=parsed.action,
                    draft=draft,
                    record=record,
                    token_hash=token_hash,
                )

        return CallbackAuthorizationResult.allow(
            action=parsed.action,
            draft=draft,
            record=record,
            token_hash=token_hash,
        )

    async def invalidate_preview_tokens(self, draft_id: str) -> int:
        """Expire all preview tokens for a draft (e.g. before a new revision)."""

        return await self._tokens.invalidate_draft_preview_tokens(
            draft_id, at=self._now()
        )

    async def issue_preview_buttons(
        self,
        draft: Draft,
        *,
        actions: Sequence[CallbackAction],
        preview_message_id: int,
        message_thread_id: int | None = None,
        ttl: timedelta | None = None,
        invalidate_previous: bool = True,
    ) -> Mapping[CallbackAction, IssuedCallbackButton]:
        """Issue bound tokens for a new preview revision.

        When ``invalidate_previous`` is true (default), all older tokens for the
        draft are expired first so old buttons fail closed.
        """

        if preview_message_id <= 0:
            raise ValueError("preview_message_id must be positive")
        if not actions:
            raise ValueError("actions must not be empty")

        now = self._now()
        if invalidate_previous:
            await self._tokens.invalidate_draft_preview_tokens(draft.draft_id, at=now)

        expires_at = now + (ttl if ttl is not None else self._preview_token_ttl)
        thread_id = (
            message_thread_id
            if message_thread_id is not None
            else draft.message_thread_id
        )

        issued: dict[CallbackAction, IssuedCallbackButton] = {}
        for action in actions:
            opaque = generate_opaque_token()
            record = build_token_record(
                opaque_token=opaque,
                draft_id=draft.draft_id,
                owner_user_id=draft.owner_id,
                chat_id=draft.chat_id,
                action=action,
                expected_revision=draft.revision,
                expected_state=draft.state.value,
                expires_at=expires_at,
                message_thread_id=thread_id,
                preview_message_id=preview_message_id,
            )
            await self._tokens.store_callback(record)
            callback_data = encode_callback_data(action, opaque)
            issued[action] = IssuedCallbackButton(
                action=action,
                callback_data=callback_data,
                token_hash=record.token_hash,
                record=record,
            )
        return issued

    async def on_preview_revision_committed(self, draft_id: str) -> int:
        """Hook for workflow services after a new preview revision is saved."""

        return await self.invalidate_preview_tokens(draft_id)
