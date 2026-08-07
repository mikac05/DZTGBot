"""Atomic, hot-reloadable runtime Jira rules storage."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class RulesStoreError(RuntimeError):
    """Raised when runtime rules cannot be loaded or updated safely."""


class RulesStore:
    """Maintain disk-backed rules with an in-memory last-known-good fallback."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._backup_path = path.with_name(f"{path.name}.previous")
        self._current: str | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Load the primary rules file, falling back to the previous saved version."""

        async with self._lock:
            try:
                loaded = await asyncio.to_thread(self._read_validated, self._path)
            except RulesStoreError as primary_error:
                try:
                    loaded = await asyncio.to_thread(self._read_validated, self._backup_path)
                except RulesStoreError as backup_error:
                    raise RulesStoreError(
                        "Neither the runtime rules file nor its previous version could be loaded."
                    ) from backup_error
                LOGGER.warning(
                    "Primary rules file could not be loaded; using previous rules (%s)",
                    type(primary_error).__name__,
                )
                try:
                    await asyncio.to_thread(self._atomic_write, self._path, loaded)
                except Exception:
                    LOGGER.exception(
                        "Could not restore the primary rules file; continuing with previous rules"
                    )
            else:
                await asyncio.to_thread(self._atomic_write, self._backup_path, loaded)

            self._current = loaded

    async def current_rules(self) -> str:
        """Return current rules and hot-reload a valid external file change."""

        async with self._lock:
            if self._current is None:
                raise RulesStoreError("Rules store has not been initialized.")

            try:
                loaded = await asyncio.to_thread(self._read_validated, self._path)
            except RulesStoreError as error:
                LOGGER.error(
                    "Rules reload failed; retaining the last-known-good rules (%s)",
                    type(error).__name__,
                )
                return self._current

            if loaded != self._current:
                await asyncio.to_thread(self._atomic_write, self._backup_path, self._current)
                self._current = loaded
                LOGGER.info("Reloaded runtime Jira rules from disk")

            return self._current

    async def replace(self, new_rules: str) -> None:
        """Atomically replace rules and immediately activate the validated result."""

        validated = self._validate(new_rules)
        async with self._lock:
            if self._current is None:
                raise RulesStoreError("Rules store has not been initialized.")

            previous = self._current
            try:
                await asyncio.to_thread(self._atomic_write, self._backup_path, previous)
                await asyncio.to_thread(self._atomic_write, self._path, validated)
                reloaded = await asyncio.to_thread(self._read_validated, self._path)
            except Exception as error:
                try:
                    await asyncio.to_thread(self._atomic_write, self._path, previous)
                except Exception:
                    LOGGER.exception("Could not restore the primary rules file after update failure")
                self._current = previous
                raise RulesStoreError(
                    "The new rules could not be saved; the previous rules remain active."
                ) from error

            self._current = reloaded
            LOGGER.info("Runtime Jira rules updated and hot-reloaded")

    @staticmethod
    def _validate(rules: str) -> str:
        normalized = rules.strip()
        if not normalized:
            raise RulesStoreError("Rules must not be empty.")
        return normalized

    @classmethod
    def _read_validated(cls, path: Path) -> str:
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_descriptor = os.open(path, flags)
            with os.fdopen(file_descriptor, "r", encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise RulesStoreError("Rules path is not a regular file.")
                content = handle.read()
        except (OSError, UnicodeError) as error:
            raise RulesStoreError(f"Could not read rules file: {path}") from error
        return cls._validate(content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{content.rstrip()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
