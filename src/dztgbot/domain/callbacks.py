"""Strict callback grammar, opaque tokens, and authorization records.

Pure domain module: no Telegram, provider SDK, or infrastructure imports.

Callback wire format (Telegram ``callback_data``, max 64 bytes)::

    j1:<short-action>:<opaque-token>

The opaque token carries at least 128 bits of cryptographic randomness.
Only a SHA-256 hash of the token is suitable for durable storage.
Possession of a valid token is never sufficient authorization by itself;
actor, chat, thread, preview message, action, state, revision, expiry,
and one-shot status must still be verified by the authorization service.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


# Telegram Bot API limit for callback_data.
CALLBACK_DATA_MAX_LENGTH = 64

# Wire protocol version prefix (fixed for this generation of buttons).
CALLBACK_VERSION = "j1"

# Opaque token: 16 bytes = 128 bits, encoded as 32 lowercase hex characters.
OPAQUE_TOKEN_BYTES = 16
OPAQUE_TOKEN_HEX_LENGTH = OPAQUE_TOKEN_BYTES * 2
OPAQUE_TOKEN_PATTERN = re.compile(rf"\A[0-9a-f]{{{OPAQUE_TOKEN_HEX_LENGTH}}}\Z")

# Short action codes: lowercase letters only, length 2–4.
ACTION_PATTERN = re.compile(r"\A[a-z]{2,4}\Z")

# Full callback_data: version, action, token — no extra segments.
CALLBACK_DATA_PATTERN = re.compile(
    rf"\A{re.escape(CALLBACK_VERSION)}:"
    rf"(?P<action>[a-z]{{2,4}}):"
    rf"(?P<token>[0-9a-f]{{{OPAQUE_TOKEN_HEX_LENGTH}}})\Z"
)

# SHA-256 hex digest length for stored token hashes.
TOKEN_HASH_HEX_LENGTH = 64


class CallbackAction(str, Enum):
    """Allowlisted short actions embedded in callback_data."""

    CONFIRM = "cfm"
    EDIT = "edt"
    CANCEL = "cnl"
    COPY_LINK = "cpl"
    COPY_SUMMARY = "cps"
    EDIT_PUBLISHED = "edp"
    TOGGLE_TYPE = "ttyp"
    TOGGLE_PRIORITY = "tpri"
    RETRY = "rty"
    RECONCILE = "rcn"
    MOVE_TRANSITION = "mv"
    SELECT_TRANSITION = "smv"
    QUICK_EDIT = "qed"
    ADD_COMMENT = "cmt"
    BLOCK_ISSUE = "blk"
    UNBLOCK_ISSUE = "ubk"
    ASSIGN_ISSUE = "asn"
    ASSIGN_ME = "asme"
    CREATE_SUBTASK = "sub"
    QUICK_FILTER = "flt"
    REFRESH_CARD = "rfh"
    WATCH_ISSUE = "wtc"
    UNWATCH_ISSUE = "uwtc"

    @classmethod
    def allowlist(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


# Actions that must be one-shot by default (consume after successful authorize).
ONE_SHOT_ACTIONS: frozenset[CallbackAction] = frozenset(
    {
        CallbackAction.CONFIRM,
        CallbackAction.CANCEL,
        CallbackAction.RETRY,
        CallbackAction.RECONCILE,
        CallbackAction.EDIT_PUBLISHED,
        CallbackAction.SELECT_TRANSITION,
        CallbackAction.BLOCK_ISSUE,
        CallbackAction.UNBLOCK_ISSUE,
        CallbackAction.ASSIGN_ME,
    }
)


class CallbackParseError(ValueError):
    """Raised when callback_data fails strict grammar validation.

    ``code`` is a fixed machine-safe label. The exception message must never
    include the raw attacker-controlled callback payload.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParsedCallback:
    """Result of successfully parsing a callback_data string."""

    version: str
    action: CallbackAction
    opaque_token: str

    def encode(self) -> str:
        """Serialize back to wire format (never longer than the Telegram limit)."""

        return encode_callback_data(self.action, self.opaque_token)


@dataclass(frozen=True, slots=True)
class CallbackTokenRecord:
    """Durable authorization record for one rendered button action.

    The raw opaque token is never stored — only ``token_hash``.
    """

    token_hash: str
    draft_id: str
    owner_user_id: int
    chat_id: int
    message_thread_id: int | None
    preview_message_id: int | None
    expected_revision: int
    expected_state: str
    action: CallbackAction
    expires_at: datetime
    one_shot: bool
    consumed_at: datetime | None = None

    def __post_init__(self) -> None:
        if len(self.token_hash) != TOKEN_HASH_HEX_LENGTH:
            raise ValueError("token_hash must be a SHA-256 hex digest")
        if not re.fullmatch(r"[0-9a-f]+", self.token_hash):
            raise ValueError("token_hash must be lowercase hex")
        if not self.draft_id:
            raise ValueError("draft_id must not be empty")
        if self.owner_user_id <= 0:
            raise ValueError("owner_user_id must be positive")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if self.expected_revision < 1:
            raise ValueError("expected_revision must be >= 1")
        if not self.expected_state:
            raise ValueError("expected_state must not be empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (UTC)")
        if self.consumed_at is not None and self.consumed_at.tzinfo is None:
            raise ValueError("consumed_at must be timezone-aware (UTC)")


@dataclass(frozen=True, slots=True)
class CallbackAuthorizationInput:
    """Caller-supplied context for authorizing a parsed callback.

    Does not include the raw callback_data string so policy layers cannot
    accidentally echo attacker-controlled payloads into logs or replies.
    """

    actor_user_id: int
    chat_id: int
    chat_type: str
    message_thread_id: int | None
    preview_message_id: int | None
    action: CallbackAction
    opaque_token: str
    now: datetime

    def __post_init__(self) -> None:
        if self.actor_user_id <= 0:
            raise ValueError("actor_user_id must be positive")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")
        if not OPAQUE_TOKEN_PATTERN.fullmatch(self.opaque_token):
            raise ValueError("opaque_token failed alphabet/length checks")


def generate_opaque_token(*, nbytes: int = OPAQUE_TOKEN_BYTES) -> str:
    """Return a new opaque token with at least 128 bits of randomness.

    Tokens are lowercase hex so the callback alphabet stays tightly allowlisted.
    """

    if nbytes < OPAQUE_TOKEN_BYTES:
        raise ValueError(
            f"opaque tokens must be at least {OPAQUE_TOKEN_BYTES} bytes "
            f"({OPAQUE_TOKEN_BYTES * 8} bits)"
        )
    return secrets.token_hex(nbytes)


def hash_opaque_token(opaque_token: str) -> str:
    """Return the SHA-256 hex digest used for durable token storage."""

    if not OPAQUE_TOKEN_PATTERN.fullmatch(opaque_token):
        # Reject non-canonical tokens rather than hashing attacker garbage.
        raise CallbackParseError("callback_token_alphabet")
    return hashlib.sha256(opaque_token.encode("ascii")).hexdigest()


def encode_callback_data(action: CallbackAction | str, opaque_token: str) -> str:
    """Build wire-format callback_data or raise if it would violate grammar."""

    action_value = action.value if isinstance(action, CallbackAction) else action
    if action_value not in CallbackAction.allowlist():
        raise CallbackParseError("callback_action_unknown")
    if not OPAQUE_TOKEN_PATTERN.fullmatch(opaque_token):
        raise CallbackParseError("callback_token_alphabet")

    encoded = f"{CALLBACK_VERSION}:{action_value}:{opaque_token}"
    if len(encoded) > CALLBACK_DATA_MAX_LENGTH:
        raise CallbackParseError("callback_data_too_long")
    # Re-parse to guarantee encode/decode symmetry and alphabet rules.
    parse_callback_data(encoded)
    return encoded


def parse_callback_data(raw: str | None) -> ParsedCallback:
    """Strictly parse callback_data.

    On failure raises :class:`CallbackParseError` with a fixed ``code`` only —
    never includes ``raw`` in the exception message.
    """

    if raw is None:
        raise CallbackParseError("callback_data_missing")
    if not isinstance(raw, str):
        raise CallbackParseError("callback_data_type")
    if raw == "":
        raise CallbackParseError("callback_data_empty")
    if len(raw) > CALLBACK_DATA_MAX_LENGTH:
        raise CallbackParseError("callback_data_too_long")
    # Reject non-ASCII early so we never process overlong/odd encodings.
    try:
        raw.encode("ascii")
    except UnicodeEncodeError as error:
        raise CallbackParseError("callback_data_non_ascii") from error

    match = CALLBACK_DATA_PATTERN.fullmatch(raw)
    if match is None:
        # Distinguish a few structural cases without echoing content.
        if not raw.startswith(f"{CALLBACK_VERSION}:"):
            raise CallbackParseError("callback_version_unsupported")
        parts = raw.split(":")
        if len(parts) != 3:
            raise CallbackParseError("callback_segment_count")
        _, action_part, token_part = parts
        if not ACTION_PATTERN.fullmatch(action_part):
            raise CallbackParseError("callback_action_alphabet")
        if action_part not in CallbackAction.allowlist():
            raise CallbackParseError("callback_action_unknown")
        if not OPAQUE_TOKEN_PATTERN.fullmatch(token_part):
            raise CallbackParseError("callback_token_alphabet")
        raise CallbackParseError("callback_data_malformed")

    action_value = match.group("action")
    if action_value not in CallbackAction.allowlist():
        raise CallbackParseError("callback_action_unknown")

    return ParsedCallback(
        version=CALLBACK_VERSION,
        action=CallbackAction(action_value),
        opaque_token=match.group("token"),
    )


def default_one_shot(action: CallbackAction) -> bool:
    """Return whether an action should consume its token after use."""

    return action in ONE_SHOT_ACTIONS


def build_token_record(
    *,
    opaque_token: str,
    draft_id: str,
    owner_user_id: int,
    chat_id: int,
    action: CallbackAction,
    expected_revision: int,
    expected_state: str,
    expires_at: datetime,
    message_thread_id: int | None = None,
    preview_message_id: int | None = None,
    one_shot: bool | None = None,
) -> CallbackTokenRecord:
    """Construct a storeable token record from a newly issued opaque token."""

    return CallbackTokenRecord(
        token_hash=hash_opaque_token(opaque_token),
        draft_id=draft_id,
        owner_user_id=owner_user_id,
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        preview_message_id=preview_message_id,
        expected_revision=expected_revision,
        expected_state=expected_state,
        action=action,
        expires_at=expires_at,
        one_shot=default_one_shot(action) if one_shot is None else one_shot,
        consumed_at=None,
    )
