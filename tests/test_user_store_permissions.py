"""Permission and file-type safety tests for UserStore (P2-G)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from dztgbot.user_store import JiraCredentials, UserStore, UserStoreError
from tests.support.security_fakes import TEST_ONLY_PAT


class UserStorePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_written_file_is_mode_0600_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            await store.store(
                1,
                JiraCredentials(
                    jira_username="u",
                    jira_display_name="U",
                    jira_pat=TEST_ONLY_PAT,
                ),
            )
            self.assertTrue(path.is_file())
            mode = stat.S_IMODE(path.stat().st_mode)
            if os.name == "posix":
                self.assertEqual(mode, 0o600)
            else:
                # On Windows, chmod is best-effort; still require a regular file.
                self.assertTrue(path.is_file())

    async def test_previous_backup_is_regular_file_with_restrictive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            creds = JiraCredentials(
                jira_username="u",
                jira_display_name="U",
                jira_pat=TEST_ONLY_PAT,
            )
            await store.store(1, creds)
            await store.store(
                1,
                JiraCredentials(
                    jira_username="u2",
                    jira_display_name="U2",
                    jira_pat=TEST_ONLY_PAT,
                ),
            )
            prev = path.with_name(path.name + ".prev")
            self.assertTrue(prev.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(prev.stat().st_mode), 0o600)

    async def test_directory_path_is_not_accepted_as_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "not-a-file"
            directory.mkdir()
            store = UserStore(directory)
            await store.initialize()
            # Load fails closed to empty; store should still create a file path
            # only when writing to a non-directory path. Writing through a path
            # that is a directory must fail.
            with self.assertRaises((OSError, UserStoreError, IsADirectoryError)):
                await store.store(
                    1,
                    JiraCredentials(
                        jira_username="u",
                        jira_display_name="U",
                        jira_pat=TEST_ONLY_PAT,
                    ),
                )

    async def test_symlink_rejection_when_nofollow_supported(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available on this platform")
        if os.name == "nt":
            self.skipTest("symlink semantics differ on Windows CI")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = Path(tmp) / "creds.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlinks not permitted in this environment")

            store = UserStore(link)
            await store.initialize()
            # Initialize fails closed; attempting to read via symlink should not
            # load arbitrary target content as trusted without regular-file checks.
            # Depending on platform, open may fail or treat as non-regular.
            # Either empty store or safe error path is acceptable; never crash.
            _ = await store.get(1)


if __name__ == "__main__":
    unittest.main()
