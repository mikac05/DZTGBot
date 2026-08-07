"""First-release security policies and safe denial codes.

Pure domain module: no Telegram, provider SDK, or infrastructure imports.

Policies encoded here (MASTER_PLAN §2 and P1-G):

* private-chat-only for workflows, auth, and admin
* PAT-only credential input (reject password, Basic, session cookie shapes)
* actor / chat / thread / preview-message binding for callbacks
* fixed, non-leaky denial codes for user-visible feedback
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .callbacks import (
    CallbackAction,
    CallbackAuthorizationInput,
    CallbackParseError,
    CallbackTokenRecord,
    hash_opaque_token,
)


# Auth conversation lifetime for credential collection (MASTER_PLAN decision #2).
AUTH_CONVERSATION_TTL = timedelta(minutes=3)

# Telegram chat.type values treated as private.
_PRIVATE_CHAT_TYPES = frozenset({"private"})

# Chat types that are never allowed for mutating first-release workflows.
_NON_PRIVATE_CHAT_TYPES = frozenset(
    {"group", "supergroup", "channel", "sender"}
)


class DenialCode(str, Enum):
    """Fixed machine-safe denial codes.

    User-facing text must be derived from these codes only. Never embed
    raw callback payloads, tokens, message text, or credentials in replies.
    """

    # Chat / audience
    NOT_PRIVATE_CHAT = "not_private_chat"
    NOT_ALLOWED_USER = "not_allowed_user"
    NOT_ADMIN = "not_admin"

    # Callback authorization
    MALFORMED_CALLBACK = "malformed_callback"
    UNKNOWN_TOKEN = "unknown_token"
    FOREIGN_ACTOR = "foreign_actor"
    WRONG_CHAT = "wrong_chat"
    WRONG_THREAD = "wrong_thread"
    WRONG_MESSAGE = "wrong_message"
    ACTION_MISMATCH = "action_mismatch"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_CONSUMED = "token_consumed"
    STALE_REVISION = "stale_revision"
    ILLEGAL_STATE = "illegal_state"
    ALREADY_PROCESSING = "already_processing"

    # Credentials
    CREDENTIAL_EMPTY = "credential_empty"
    CREDENTIAL_FORMAT_REJECTED = "credential_format_rejected"
    AUTH_EXPIRED = "auth_expired"
    CREDENTIAL_DELETE_FAILED = "credential_delete_failed"

    # Generic safe fallback
    DENIED = "denied"


class CredentialInputKind(str, Enum):
    """Classification of a user-submitted credential string."""

    PAT = "pat"
    REJECTED_PASSWORD = "rejected_password"
    REJECTED_BASIC = "rejected_basic"
    REJECTED_COOKIE = "rejected_cookie"
    REJECTED_EMPTY = "rejected_empty"
    REJECTED_OTHER = "rejected_other"


class PolicyDecisionKind(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of a pure policy evaluation."""

    kind: PolicyDecisionKind
    denial_code: DenialCode | None = None

    @property
    def allowed(self) -> bool:
        return self.kind is PolicyDecisionKind.ALLOW

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(kind=PolicyDecisionKind.ALLOW)

    @classmethod
    def deny(cls, code: DenialCode) -> PolicyDecision:
        return cls(kind=PolicyDecisionKind.DENY, denial_code=code)


# Stable, non-leaky user-visible messages for denial codes (Taiwan Traditional Chinese
# for primary bot UX where applicable; English codes remain the API).
_DENIAL_USER_MESSAGES: dict[DenialCode, str] = {
    DenialCode.NOT_PRIVATE_CHAT: "此操作僅限與機器人的私聊視窗使用。",
    DenialCode.NOT_ALLOWED_USER: "您沒有權限使用此機器人。",
    DenialCode.NOT_ADMIN: "您沒有權限管理此機器人。",
    DenialCode.MALFORMED_CALLBACK: "此按鈕無效或已過期。",
    DenialCode.UNKNOWN_TOKEN: "此按鈕無效或已過期。",
    DenialCode.FOREIGN_ACTOR: "您無法操作其他人的工單草稿。",
    DenialCode.WRONG_CHAT: "此按鈕不適用於目前的聊天。",
    DenialCode.WRONG_THREAD: "此按鈕不適用於目前的討論串。",
    DenialCode.WRONG_MESSAGE: "此按鈕已失效，請使用最新的預覽訊息。",
    DenialCode.ACTION_MISMATCH: "此按鈕無效或已過期。",
    DenialCode.TOKEN_EXPIRED: "此按鈕已逾時，請重新取得預覽。",
    DenialCode.TOKEN_CONSUMED: "此操作已處理，請勿重複點擊。",
    DenialCode.STALE_REVISION: "草稿已更新，請使用最新的預覽按鈕。",
    DenialCode.ILLEGAL_STATE: "目前狀態無法執行此操作。",
    DenialCode.ALREADY_PROCESSING: "正在處理中，請稍候。",
    DenialCode.CREDENTIAL_EMPTY: "憑據不能為空。",
    DenialCode.CREDENTIAL_FORMAT_REJECTED: (
        "僅支援 Jira 個人存取令牌 (PAT)。"
        "帳號密碼與 Session Cookie 已停用。"
    ),
    DenialCode.AUTH_EXPIRED: "綁定操作已逾時，請重新開始 /auth。",
    DenialCode.CREDENTIAL_DELETE_FAILED: (
        "無法自動刪除含有憑據的訊息，請立即手動刪除該訊息。"
    ),
    DenialCode.DENIED: "操作被拒絕。",
}


def user_message_for_denial(code: DenialCode) -> str:
    """Return a fixed user-visible string for a denial code (never echoes inputs)."""

    return _DENIAL_USER_MESSAGES.get(code, _DENIAL_USER_MESSAGES[DenialCode.DENIED])


def is_private_chat(chat_type: str | None) -> bool:
    """Return True only for Telegram private chats."""

    if chat_type is None:
        return False
    return chat_type.casefold() in _PRIVATE_CHAT_TYPES


def require_private_chat(chat_type: str | None) -> PolicyDecision:
    """First-release gate: workflows, auth, and admin require a private chat."""

    if is_private_chat(chat_type):
        return PolicyDecision.allow()
    return PolicyDecision.deny(DenialCode.NOT_PRIVATE_CHAT)


def require_allowed_user(
    actor_user_id: int,
    allowed_user_ids: frozenset[int] | None,
) -> PolicyDecision:
    """Optional deployment allowlist. ``None`` or empty means unrestricted."""

    if not allowed_user_ids:
        return PolicyDecision.allow()
    if actor_user_id in allowed_user_ids:
        return PolicyDecision.allow()
    return PolicyDecision.deny(DenialCode.NOT_ALLOWED_USER)


def require_admin(
    actor_user_id: int,
    admin_user_ids: frozenset[int],
) -> PolicyDecision:
    """Administrator numeric-ID gate for /rules, /setrules, /vpn, /vpnstart."""

    if actor_user_id in admin_user_ids:
        return PolicyDecision.allow()
    return PolicyDecision.deny(DenialCode.NOT_ADMIN)


def require_private_admin(
    chat_type: str | None,
    actor_user_id: int,
    admin_user_ids: frozenset[int],
) -> PolicyDecision:
    """Admin commands must be both authorized and private-chat-only."""

    private = require_private_chat(chat_type)
    if not private.allowed:
        return private
    return require_admin(actor_user_id, admin_user_ids)


def auth_deadline(started_at: datetime) -> datetime:
    """Return the absolute UTC deadline for an auth conversation."""

    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware (UTC)")
    return started_at + AUTH_CONVERSATION_TTL


def is_auth_expired(started_at: datetime, now: datetime) -> bool:
    """Return True when the PAT collection conversation has exceeded its TTL."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    return now >= auth_deadline(started_at)


def classify_credential_input(raw: str | None) -> CredentialInputKind:
    """Classify credential text for PAT-only enforcement.

    This does not validate the token against Jira; it only rejects known
    non-PAT shapes (passwords, Basic headers, session cookies).
    """

    if raw is None:
        return CredentialInputKind.REJECTED_EMPTY
    text = raw.strip()
    if not text:
        return CredentialInputKind.REJECTED_EMPTY

    lower = text.casefold()

    # Explicit Basic header or scheme.
    if lower.startswith("basic "):
        return CredentialInputKind.REJECTED_BASIC

    # Browser / server session cookie material.
    if "jsessionid=" in lower or lower.startswith("jsessionid"):
        return CredentialInputKind.REJECTED_COOKIE
    if "set-cookie:" in lower:
        return CredentialInputKind.REJECTED_COOKIE

    # username:password (and similar) — colon present outside Bearer prefix.
    if lower.startswith("bearer "):
        token_body = text[7:].strip()
        if not token_body:
            return CredentialInputKind.REJECTED_EMPTY
        # Bearer payload must not itself look like user:pass or cookie.
        nested = classify_credential_input(token_body)
        if nested is CredentialInputKind.PAT:
            return CredentialInputKind.PAT
        return nested

    # Raw Basic base64 blobs are hard to detect; reject obvious user:pass.
    if ":" in text:
        return CredentialInputKind.REJECTED_PASSWORD

    # Remaining non-empty token-like strings are treated as PAT candidates.
    return CredentialInputKind.PAT


def normalize_pat_input(raw: str) -> str | None:
    """Return the PAT string to store/validate, or None if rejected/empty.

    Strips an optional ``Bearer `` prefix. Never returns password/cookie shapes.
    """

    kind = classify_credential_input(raw)
    if kind is CredentialInputKind.REJECTED_EMPTY:
        return None
    if kind is not CredentialInputKind.PAT:
        return None
    text = raw.strip()
    if text.casefold().startswith("bearer "):
        return text[7:].strip() or None
    return text


def credential_policy_decision(raw: str | None) -> PolicyDecision:
    """Policy gate for credential submission (shape only)."""

    kind = classify_credential_input(raw)
    if kind is CredentialInputKind.PAT:
        return PolicyDecision.allow()
    if kind is CredentialInputKind.REJECTED_EMPTY:
        return PolicyDecision.deny(DenialCode.CREDENTIAL_EMPTY)
    return PolicyDecision.deny(DenialCode.CREDENTIAL_FORMAT_REJECTED)


def authorize_callback(
    request: CallbackAuthorizationInput,
    record: CallbackTokenRecord | None,
    *,
    current_revision: int | None = None,
    current_state: str | None = None,
) -> PolicyDecision:
    """Evaluate callback authorization against a loaded token record.

    Order of checks is intentional: private chat and allowlisting are
    environmental; identity binding precedes token lifecycle; state/revision
    checks run last so stale UI gets a specific code when possible.

    Possession of a matching token hash is **never** sufficient alone.
    """

    private = require_private_chat(request.chat_type)
    if not private.allowed:
        return private

    if record is None:
        return PolicyDecision.deny(DenialCode.UNKNOWN_TOKEN)

    # Bind token material without revealing mismatches in detail beyond codes.
    try:
        presented_hash = hash_opaque_token(request.opaque_token)
    except CallbackParseError:
        return PolicyDecision.deny(DenialCode.MALFORMED_CALLBACK)
    if presented_hash != record.token_hash:
        return PolicyDecision.deny(DenialCode.UNKNOWN_TOKEN)

    if request.action is not record.action:
        return PolicyDecision.deny(DenialCode.ACTION_MISMATCH)

    if request.actor_user_id != record.owner_user_id:
        return PolicyDecision.deny(DenialCode.FOREIGN_ACTOR)

    if request.chat_id != record.chat_id:
        return PolicyDecision.deny(DenialCode.WRONG_CHAT)

    if record.message_thread_id is not None:
        if request.message_thread_id != record.message_thread_id:
            return PolicyDecision.deny(DenialCode.WRONG_THREAD)

    if record.preview_message_id is not None:
        if request.preview_message_id != record.preview_message_id:
            return PolicyDecision.deny(DenialCode.WRONG_MESSAGE)

    if request.now >= record.expires_at:
        return PolicyDecision.deny(DenialCode.TOKEN_EXPIRED)

    if record.consumed_at is not None:
        return PolicyDecision.deny(DenialCode.TOKEN_CONSUMED)

    if current_revision is not None and current_revision != record.expected_revision:
        return PolicyDecision.deny(DenialCode.STALE_REVISION)

    if current_state is not None and current_state != record.expected_state:
        # Submitting while a confirm is re-clicked is a distinct UX code.
        # Compare against the canonical FSM wire value ``submitting``.
        normalized = current_state.casefold() if isinstance(current_state, str) else str(current_state)
        if (
            normalized == "submitting"
            and request.action is CallbackAction.CONFIRM
        ):
            return PolicyDecision.deny(DenialCode.ALREADY_PROCESSING)
        return PolicyDecision.deny(DenialCode.ILLEGAL_STATE)

    return PolicyDecision.allow()


def may_disclose_jira_identity(chat_type: str | None) -> bool:
    """Whether /start (or similar) may show Jira display name / username."""

    return is_private_chat(chat_type)


def may_disclose_runtime_rules(chat_type: str | None) -> bool:
    """Whether /rules may return rule text (private admin only; shape check)."""

    return is_private_chat(chat_type)


def logout_revokes_remote_pat() -> bool:
    """Documented truth: local logout does not revoke the PAT at Jira."""

    return False
