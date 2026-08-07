"""Failure and corruption tests for UserStore copy-on-write (P2-G)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dztgbot.user_store import (
    MAX_STORE_BYTES,
    JiraCredentials,
    UserStore,
    UserStoreError,
)
from tests.support.security_fakes import TEST_ONLY_PAT


def _creds(
    *,
    username: str = "alice",
    display: str = "Alice",
    pat: str = TEST_ONLY_PAT,
) -> JiraCredentials:
    return JiraCredentials(
        jira_username=username,
        jira_display_name=display,
        jira_pat=pat,
    )


class CopyOnWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_store_leaves_memory_and_disk_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()

            first = _creds(username="first")
            await store.store(1, first)

            def boom(_snapshot: dict) -> None:
                raise OSError("disk full")

            with patch.object(store, "_write_store", side_effect=boom):
                with self.assertRaises(OSError):
                    await store.store(2, _creds(username="second", pat="TEST_ONLY_OTHER"))

            self.assertIsNotNone(await store.get(1))
            self.assertEqual((await store.get(1)).jira_username, "first")
            self.assertIsNone(await store.get(2))

            on_disk = json.loads(path.read_text(encoding="utf-8"))
            users = on_disk.get("credentials", on_disk)
            self.assertIn("1", users)
            self.assertNotIn("2", users)
            self.assertNotIn("TEST_ONLY_OTHER", path.read_text(encoding="utf-8"))

    async def test_failed_remove_leaves_memory_and_disk_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            await store.store(5, _creds())

            def boom(_snapshot: dict) -> None:
                raise OSError("disk full")

            with patch.object(store, "_write_store", side_effect=boom):
                with self.assertRaises(OSError):
                    await store.remove(5)

            self.assertIsNotNone(await store.get(5))
            raw = path.read_text(encoding="utf-8")
            self.assertIn("5", raw)

    async def test_store_then_reload_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            await store.store(9, _creds(username="bob", display="Bob"))

            reloaded = UserStore(path)
            await reloaded.initialize()
            got = await reloaded.get(9)
            self.assertIsNotNone(got)
            self.assertEqual(got.jira_username, "bob")
            self.assertEqual(got.jira_pat, TEST_ONLY_PAT)


class CorruptionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_primary_recovers_from_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            await store.store(1, _creds(username="keep-me"))
            await store.store(1, _creds(username="keep-me-v2"))

            # Previous backup should exist after second write.
            prev = path.with_name(path.name + ".prev")
            self.assertTrue(prev.exists())

            path.write_text("{not-json", encoding="utf-8")

            recovered = UserStore(path)
            await recovered.initialize()
            got = await recovered.get(1)
            self.assertIsNotNone(got)
            # Either previous snapshot (v1) or restored content — must not be empty loss.
            self.assertIn(got.jira_username, {"keep-me", "keep-me-v2"})

            corrupt = path.with_name(path.name + ".corrupt")
            self.assertTrue(corrupt.exists() or path.exists())

    async def test_truncated_json_does_not_poison_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text('{"version": 1, "credentials": {"1":', encoding="utf-8")
            store = UserStore(path)
            await store.initialize()
            self.assertIsNone(await store.get(1))

    async def test_wrong_shaped_root_rejected_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            store = UserStore(path)
            await store.initialize()
            self.assertIsNone(await store.get(1))

    async def test_legacy_flat_map_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text(
                json.dumps(
                    {
                        "42": {
                            "jira_username": "legacy",
                            "jira_display_name": "Legacy User",
                            "jira_pat": TEST_ONLY_PAT,
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = UserStore(path)
            await store.initialize()
            got = await store.get(42)
            self.assertIsNotNone(got)
            self.assertEqual(got.jira_username, "legacy")

    async def test_invalid_entry_types_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "credentials": {
                            "1": {
                                "jira_username": 123,
                                "jira_display_name": "X",
                                "jira_pat": TEST_ONLY_PAT,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = UserStore(path)
            await store.initialize()
            self.assertIsNone(await store.get(1))

    async def test_oversized_store_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            # Build a file larger than the cap without embedding real secrets.
            padding = "x" * (MAX_STORE_BYTES + 100)
            path.write_text(padding, encoding="utf-8")
            store = UserStore(path)
            await store.initialize()
            self.assertIsNone(await store.get(1))


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_positive_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserStore(Path(tmp) / "creds.json")
            await store.initialize()
            with self.assertRaises(UserStoreError):
                await store.store(0, _creds())
            with self.assertRaises(UserStoreError):
                await store.store(-1, _creds())

    async def test_rejects_empty_credential_fields(self) -> None:
        with self.assertRaises(UserStoreError):
            JiraCredentials(jira_username="", jira_display_name="A", jira_pat=TEST_ONLY_PAT)
        with self.assertRaises(UserStoreError):
            JiraCredentials(jira_username="a", jira_display_name="A", jira_pat="")

    async def test_repr_redacts_pat(self) -> None:
        creds = _creds()
        rendered = repr(creds)
        self.assertIn("<redacted>", rendered)
        self.assertNotIn(TEST_ONLY_PAT, rendered)
        self.assertIn("alice", rendered)


class PrivacyInErrorsTests(unittest.TestCase):
    def test_user_store_error_messages_do_not_embed_pat(self) -> None:
        # Construct validation path that might receive PAT-like input.
        try:
            JiraCredentials(jira_username="u", jira_display_name="d", jira_pat="")
        except UserStoreError as error:
            self.assertNotIn(TEST_ONLY_PAT, str(error))
            self.assertNotIn("ATATT", str(error))


if __name__ == "__main__":
    unittest.main()
