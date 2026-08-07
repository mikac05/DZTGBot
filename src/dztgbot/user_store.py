"""Per-user Jira credential storage backed by an atomic JSON file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JiraCredentials:
    """A Telegram user's stored Jira identity and access token."""

    jira_username: str
    jira_display_name: str
    jira_pat: str


class UserStoreError(RuntimeError):
    """Raised when credential storage operations fail."""


class UserStore:
    """Thread-safe, disk-backed per-user Jira credential store.

    Credentials are stored as a single JSON file with mode 0600.  The
    systemd unit confines reads and writes to the service account's
    state directory.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._credentials: dict[int, JiraCredentials] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Load existing credentials from disk."""

        async with self._lock:
            if self._path.exists():
                try:
                    loaded = await asyncio.to_thread(self._read_store)
                    self._credentials = loaded
                except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                    LOGGER.warning(
                        "Could not load existing user credentials; starting fresh (%s)",
                        type(error).__name__,
                    )
                    self._credentials = {}
            else:
                self._credentials = {}

    async def get(self, telegram_user_id: int) -> JiraCredentials | None:
        """Return stored credentials for a user, or None."""

        async with self._lock:
            return self._credentials.get(telegram_user_id)

    async def store(
        self, telegram_user_id: int, credentials: JiraCredentials
    ) -> None:
        """Store or update credentials for a user and persist to disk."""

        async with self._lock:
            self._credentials[telegram_user_id] = credentials
            await asyncio.to_thread(self._write_store, self._credentials)
            LOGGER.info(
                "Stored Jira credentials for Telegram user %s", telegram_user_id
            )

    async def remove(self, telegram_user_id: int) -> bool:
        """Remove credentials for a user.  Returns True if credentials existed."""

        async with self._lock:
            if telegram_user_id not in self._credentials:
                return False
            del self._credentials[telegram_user_id]
            await asyncio.to_thread(self._write_store, self._credentials)
            LOGGER.info(
                "Removed Jira credentials for Telegram user %s", telegram_user_id
            )
            return True

    def _read_store(self) -> dict[int, JiraCredentials]:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(self._path, flags)
        with os.fdopen(file_descriptor, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UserStoreError("Credentials path is not a regular file.")
            data = json.load(handle)

        result: dict[int, JiraCredentials] = {}
        for user_id_str, entry in data.items():
            result[int(user_id_str)] = JiraCredentials(
                jira_username=entry["jira_username"],
                jira_display_name=entry["jira_display_name"],
                jira_pat=entry["jira_pat"],
            )
        return result

    def _write_store(self, credentials: dict[int, JiraCredentials]) -> None:
        data = {
            str(user_id): {
                "jira_username": cred.jira_username,
                "jira_display_name": cred.jira_display_name,
                "jira_pat": cred.jira_pat,
            }
            for user_id, cred in credentials.items()
        }
        self._atomic_write(json.dumps(data, indent=2, ensure_ascii=False))

    def _atomic_write(self, content: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
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
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
