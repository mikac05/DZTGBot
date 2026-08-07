"""Deterministic fakes for workflow characterization (Phase 0 / P0-C).

No real Telegram, Gemini, Jira, or VPN I/O. Safe for offline CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable


class FakeClock:
    """Injectable UTC clock for deadline and TTL tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FakeClock requires timezone-aware datetimes")
        self._now = when


@dataclass
class FakePhotoSize:
    file_id: str
    file_unique_id: str = "uniq-photo-1"
    width: int = 100
    height: int = 100


@dataclass
class FakeMessage:
    """Minimal Telegram message stand-in for pure helper tests."""

    message_id: int = 1
    text: str | None = None
    caption: str | None = None
    forward_origin: object | None = None
    reply_to_message: FakeMessage | None = None
    photo: list[FakePhotoSize] | None = None
    animation: object | None = None
    audio: object | None = None
    contact: object | None = None
    dice: object | None = None
    document: object | None = None
    game: object | None = None
    paid_media: object | None = None
    poll: object | None = None
    sticker: object | None = None
    story: object | None = None
    venue: object | None = None
    location: object | None = None
    video: object | None = None
    video_note: object | None = None
    voice: object | None = None
    chat: object | None = None


def make_forwarded_text_message(
    *,
    message_id: int = 10,
    text: str = "forwarded body",
) -> FakeMessage:
    """Message that core treats as a direct forward (any non-None origin)."""

    return FakeMessage(
        message_id=message_id,
        text=text,
        forward_origin=object(),
    )


def make_reply_to_forward(
    *,
    reply_text: str = "reply body",
    forwarded_text: str = "original forward",
) -> FakeMessage:
    forwarded = make_forwarded_text_message(message_id=9, text=forwarded_text)
    return FakeMessage(
        message_id=11,
        text=reply_text,
        forward_origin=None,
        reply_to_message=forwarded,
    )


def make_ordinary_message(*, text: str = "hello") -> FakeMessage:
    return FakeMessage(message_id=12, text=text, forward_origin=None)


def make_forwarded_photo_message(*, file_id: str = "photo-file-1") -> FakeMessage:
    return FakeMessage(
        message_id=13,
        text=None,
        caption="caption",
        forward_origin=object(),
        photo=[FakePhotoSize(file_id=file_id)],
    )


@dataclass
class FakeUserData(dict):
    """dict subclass used as context.user_data stand-in."""


@dataclass
class FakeContext:
    user_data: dict[str, Any] = field(default_factory=dict)
    bot: object | None = None
    error: BaseException | None = None


@dataclass
class FakeUpdate:
    effective_message: FakeMessage | None = None
    effective_user: object | None = None
    effective_chat: object | None = None
    callback_query: object | None = None


def make_user(*, user_id: int = 42, full_name: str = "Test User") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, full_name=full_name, username="tester")


def make_private_chat(*, chat_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="private", title=None)


def make_group_chat(*, chat_id: int = -1001) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="group", title="Test Group")


class RecordingLogger:
    """Capture log records without depending on logging handlers."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, tuple[Any, ...]]] = []

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(("error", msg, args))

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(("info", msg, args))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(("warning", msg, args))

    def messages(self) -> list[str]:
        out: list[str] = []
        for _level, msg, args in self.records:
            try:
                out.append(msg % args if args else msg)
            except Exception:
                out.append(msg)
        return out


class ScriptedFailingDisk:
    """Helper to simulate UserStore disk write failures after memory mutation."""

    def __init__(self, fail_on_call: int = 1) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        if self.calls >= self.fail_on_call:
            raise OSError("simulated disk write failure")
