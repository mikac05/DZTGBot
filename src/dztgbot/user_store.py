"""Per-user Jira credential storage with copy-on-write disk safety.

Phase 2 (P2-G): memory is updated only after a complete validated snapshot is
persisted. Corrupt files fail closed with recovery from a previous copy when
available. PAT values are never logged or included in ``repr``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger(__name__)

# Hard cap on on-disk store size (prevents pathological memory load).
MAX_STORE_BYTES = 1_048_576  # 1 MiB

# Schema version for forward-compatible validation.
_SCHEMA_VERSION = 1

_REQUIRED_ENTRY_KEYS = frozenset(
    {"jira_username", "jira_display_name", "jira_pat"}
)


@dataclass(frozen=True, slots=True)
class JiraCredentials:
    """A Telegram user's stored Jira identity and access token."""

    jira_username: str
    jira_display_name: str
    jira_pat: str

    def __post_init__(self) -> None:
        if not isinstance(self.jira_username, str) or not self.jira_username.strip():
            raise UserStoreError("jira_username must be a non-empty string")
        if not isinstance(self.jira_display_name, str) or not self.jira_display_name.strip():
            raise UserStoreError("jira_display_name must be a non-empty string")
        if not isinstance(self.jira_pat, str) or not self.jira_pat.strip():
            raise UserStoreError("credential token must be a non-empty string")

    def __repr__(self) -> str:
        return (
            "JiraCredentials("
            f"jira_username={self.jira_username!r}, "
            f"jira_display_name={self.jira_display_name!r}, "
            "jira_pat=<redacted>)"
        )


class UserStoreError(RuntimeError):
    """Raised when credential storage operations fail.

    Messages must never include PAT or other secret material.
    """


class UserStore:
    """Async-safe, disk-backed per-user Jira credential store.

    Credentials are stored as a single JSON file with mode ``0600``. Writes use
    copy-on-write: a full snapshot is validated and written atomically, then the
    in-memory map is replaced only on success.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._credentials: dict[int, JiraCredentials] = {}
        self._languages: dict[int, str] = {}
        self._lock = asyncio.Lock()

    def _previous_path(self) -> Path:
        return self._path.with_name(self._path.name + ".prev")

    def _corrupt_path(self) -> Path:
        return self._path.with_name(self._path.name + ".corrupt")

    async def initialize(self) -> None:
        """Load existing credentials from disk with corruption recovery."""

        async with self._lock:
            self._credentials = await asyncio.to_thread(self._load_with_recovery)

    async def get(self, telegram_user_id: int) -> JiraCredentials | None:
        """Return stored credentials for a user, or None."""

        async with self._lock:
            return self._credentials.get(telegram_user_id)

    async def get_language(self, telegram_user_id: int) -> str:
        """Return stored UI language preference for a user, defaulting to 'zh_TW'."""

        async with self._lock:
            return self._languages.get(telegram_user_id, "zh_TW")

    async def set_language(self, telegram_user_id: int, language: str) -> None:
        """Store or update UI language preference for a user."""

        self._validate_user_id(telegram_user_id)
        valid_lang = language if language in ("zh_TW", "en", "zh_CN") else "zh_TW"
        async with self._lock:
            lang_snapshot = dict(self._languages)
            lang_snapshot[telegram_user_id] = valid_lang
            await asyncio.to_thread(self._write_store, self._credentials, lang_snapshot)
            self._languages = lang_snapshot

    async def store(
        self, telegram_user_id: int, credentials: JiraCredentials
    ) -> None:
        """Store or update credentials after durable write succeeds."""

        self._validate_user_id(telegram_user_id)
        if not isinstance(credentials, JiraCredentials):
            raise UserStoreError("credentials must be a JiraCredentials instance")
        # Re-run field validation (frozen dataclass may be constructed via bypass).
        JiraCredentials(
            jira_username=credentials.jira_username,
            jira_display_name=credentials.jira_display_name,
            jira_pat=credentials.jira_pat,
        )

        async with self._lock:
            snapshot = dict(self._credentials)
            snapshot[telegram_user_id] = credentials
            await asyncio.to_thread(self._write_store, snapshot)
            self._credentials = snapshot
            LOGGER.info(
                "Stored Jira credentials for Telegram user %s", telegram_user_id
            )

    async def remove(self, telegram_user_id: int) -> bool:
        """Remove credentials for a user after durable write succeeds."""

        self._validate_user_id(telegram_user_id)
        async with self._lock:
            if telegram_user_id not in self._credentials:
                return False
            snapshot = dict(self._credentials)
            del snapshot[telegram_user_id]
            await asyncio.to_thread(self._write_store, snapshot)
            self._credentials = snapshot
            LOGGER.info(
                "Removed Jira credentials for Telegram user %s", telegram_user_id
            )
            return True

    @staticmethod
    def _validate_user_id(telegram_user_id: int) -> None:
        if not isinstance(telegram_user_id, int) or isinstance(telegram_user_id, bool):
            raise UserStoreError("telegram_user_id must be an integer")
        if telegram_user_id <= 0:
            raise UserStoreError("telegram_user_id must be a positive integer")

    def _load_with_recovery(self) -> dict[int, JiraCredentials]:
        main = self._path
        prev = self._previous_path()

        if main.exists() and main.is_dir():
            LOGGER.warning(
                "Credentials path is a directory; refusing to load or replace it"
            )
            return {}

        if main.exists():
            try:
                return self._read_store_from(main)
            except (OSError, UserStoreError, json.JSONDecodeError, UnicodeError) as error:
                LOGGER.warning(
                    "Primary credential store unreadable (%s); attempting recovery",
                    type(error).__name__,
                )
                self._quarantine_corrupt(main)

        if prev.exists() and prev.is_file():
            try:
                recovered = self._read_store_from(prev)
                # Restore recovered snapshot as the primary file when possible.
                try:
                    self._write_store(recovered)
                except (OSError, UserStoreError) as write_error:
                    LOGGER.warning(
                        "Could not restore recovered credentials to primary path (%s)",
                        type(write_error).__name__,
                    )
                return recovered
            except (OSError, UserStoreError, json.JSONDecodeError, UnicodeError) as error:
                LOGGER.warning(
                    "Previous credential store unreadable (%s); starting empty",
                    type(error).__name__,
                )

        return {}

    def _quarantine_corrupt(self, path: Path) -> None:
        """Move an unreadable store aside so a previous copy can be restored."""

        if not path.exists() or not path.is_file():
            return
        target = self._corrupt_path()
        try:
            if target.exists():
                target.unlink()
            os.replace(path, target)
            LOGGER.warning("Quarantined unreadable credential store")
        except OSError as error:
            LOGGER.warning(
                "Could not quarantine unreadable credential store (%s)",
                type(error).__name__,
            )

    def _read_store(self) -> dict[int, JiraCredentials]:
        return self._read_store_from(self._path)

    def _read_store_from(self, path: Path) -> dict[int, JiraCredentials]:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(path, flags)
        try:
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise UserStoreError("Credentials path is not a regular file.")
            if file_stat.st_size > MAX_STORE_BYTES:
                raise UserStoreError("Credentials store exceeds maximum allowed size.")
            with os.fdopen(file_descriptor, "r", encoding="utf-8") as handle:
                file_descriptor = -1  # ownership transferred
                raw = handle.read(MAX_STORE_BYTES + 1)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

        if len(raw.encode("utf-8")) > MAX_STORE_BYTES:
            raise UserStoreError("Credentials store exceeds maximum allowed size.")

        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as error:
            raise UserStoreError("Credentials store contains invalid JSON.") from error

        return self._parse_store_document(data)

    def _parse_store_document(self, data: Any) -> dict[int, JiraCredentials]:
        if not isinstance(data, dict):
            raise UserStoreError("Credentials store root must be a JSON object.")

        # Optional envelope: {"version": 1, "credentials": {...}, "languages": {...}}
        if "credentials" in data:
            version = data.get("version", _SCHEMA_VERSION)
            if not isinstance(version, int) or version < 1:
                raise UserStoreError("Credentials store version is invalid.")
            payload = data["credentials"]
            if not isinstance(payload, dict):
                raise UserStoreError("Credentials payload must be a JSON object.")
        else:
            # Legacy flat map of user_id -> entry
            payload = data

        parsed_langs: dict[int, str] = {}
        if isinstance(data.get("languages"), dict):
            for u_key, l_val in data["languages"].items():
                if isinstance(l_val, str) and l_val in ("zh_TW", "en", "zh_CN"):
                    try:
                        u_id = self._parse_user_id_key(u_key)
                        parsed_langs[u_id] = l_val
                    except UserStoreError:
                        pass
        self._languages = parsed_langs

        result: dict[int, JiraCredentials] = {}
        for user_id_key, entry in payload.items():
            user_id = self._parse_user_id_key(user_id_key)
            result[user_id] = self._parse_entry(entry)
        return result

    @staticmethod
    def _parse_user_id_key(user_id_key: Any) -> int:
        if isinstance(user_id_key, bool):
            raise UserStoreError("Credential entry key is invalid.")
        if isinstance(user_id_key, int):
            user_id = user_id_key
        elif isinstance(user_id_key, str) and user_id_key.isdigit():
            user_id = int(user_id_key)
        else:
            raise UserStoreError("Credential entry key is invalid.")
        if user_id <= 0:
            raise UserStoreError("Credential entry key must be a positive integer.")
        return user_id

    @staticmethod
    def _parse_entry(entry: Any) -> JiraCredentials:
        if not isinstance(entry, Mapping):
            raise UserStoreError("Credential entry must be a JSON object.")
        missing = _REQUIRED_ENTRY_KEYS - set(entry.keys())
        if missing:
            raise UserStoreError("Credential entry is missing required fields.")
        username = entry.get("jira_username")
        display = entry.get("jira_display_name")
        pat = entry.get("jira_pat")
        if not isinstance(username, str) or not username.strip():
            raise UserStoreError("Credential entry username is invalid.")
        if not isinstance(display, str) or not display.strip():
            raise UserStoreError("Credential entry display name is invalid.")
        if not isinstance(pat, str) or not pat.strip():
            raise UserStoreError("Credential entry token field is invalid.")
        return JiraCredentials(
            jira_username=username,
            jira_display_name=display,
            jira_pat=pat,
        )

    def _serialize_store(
        self,
        credentials: dict[int, JiraCredentials],
        languages: dict[int, str] | None = None,
    ) -> str:
        lang_map = languages if languages is not None else self._languages
        payload = {
            str(user_id): {
                "jira_username": cred.jira_username,
                "jira_display_name": cred.jira_display_name,
                "jira_pat": cred.jira_pat,
            }
            for user_id, cred in sorted(credentials.items(), key=lambda item: item[0])
        }
        lang_payload = {
            str(user_id): lang
            for user_id, lang in sorted(lang_map.items(), key=lambda item: item[0])
        }
        document = {
            "version": _SCHEMA_VERSION,
            "credentials": payload,
            "languages": lang_payload,
        }
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        if len(text.encode("utf-8")) > MAX_STORE_BYTES:
            raise UserStoreError("Credentials store exceeds maximum allowed size.")
        # Round-trip validation before any disk write.
        self._parse_store_document(json.loads(text))
        return text

    def _write_store(
        self,
        credentials: dict[int, JiraCredentials],
        languages: dict[int, str] | None = None,
    ) -> None:
        content = self._serialize_store(credentials, languages)
        self._atomic_write(content)

    def _atomic_write(self, content: str) -> None:
        if self._path.exists() and self._path.is_dir():
            raise UserStoreError(
                "Credentials path must be a regular file, not a directory."
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Preserve the last good primary as .prev before replacing it.
        if self._path.exists() and self._path.is_file():
            try:
                self._copy_file_best_effort(self._path, self._previous_path())
            except OSError as error:
                LOGGER.warning(
                    "Could not refresh previous credential backup (%s)",
                    type(error).__name__,
                )

        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(
                file_descriptor, "w", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._path)
            self._fsync_directory(self._path.parent)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _copy_file_best_effort(source: Path, destination: Path) -> None:
        data = source.read_bytes()
        if len(data) > MAX_STORE_BYTES:
            raise UserStoreError("Credentials store exceeds maximum allowed size.")
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Durably record the rename in the parent directory when the OS allows."""

        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            # Windows and some network FS may not support directory fsync.
            pass
        finally:
            os.close(dir_fd)
