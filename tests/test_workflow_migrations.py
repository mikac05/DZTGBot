"""Schema, migration-history, and local-filesystem tests for workflow SQLite."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from dztgbot.infrastructure.persistence.workflow_sqlite import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    SQLiteWorkflowRepository,
    WorkflowRepositoryError,
)


class SQLiteWorkflowMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "workflows.sqlite3"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_initialization_is_versioned_idempotent_and_hardened(self) -> None:
        repository = SQLiteWorkflowRepository(
            self.database_path, busy_timeout_seconds=0.125
        )
        await repository.initialize()
        await repository.initialize()

        self.assertEqual(await repository.schema_version(), LATEST_SCHEMA_VERSION)
        self.assertEqual(await repository.journal_mode(), "wal")
        self.assertTrue(await repository.foreign_keys_enabled())
        self.assertEqual(await repository.busy_timeout_milliseconds(), 125)

        connection = sqlite3.connect(self.database_path)
        try:
            migration_count = connection.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            connection.close()

        self.assertEqual(migration_count, LATEST_SCHEMA_VERSION)
        self.assertTrue(
            {
                "workflows",
                "source_messages",
                "attachments",
                "published_issues",
                "callback_tokens",
                "submission_attempts",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "idx_workflows_expiry",
                "idx_callbacks_expiry",
                "uq_attempts_one_active_per_draft",
                "idx_attachments_draft_status",
            }.issubset(indexes)
        )

    async def test_delete_journal_mode_can_be_requested_explicitly(self) -> None:
        repository = SQLiteWorkflowRepository(self.database_path, enable_wal=False)
        await repository.initialize()

        self.assertEqual(await repository.journal_mode(), "delete")

    async def test_migration_checksum_tampering_fails_closed(self) -> None:
        repository = SQLiteWorkflowRepository(self.database_path)
        await repository.initialize()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=1",
                ("0" * 64,),
            )
            connection.commit()
        finally:
            connection.close()

        restarted = SQLiteWorkflowRepository(self.database_path)
        with self.assertRaisesRegex(
            MigrationError, "workflow_migration_checksum_mismatch"
        ):
            await restarted.initialize()

    async def test_unknown_schema_version_fails_closed(self) -> None:
        repository = SQLiteWorkflowRepository(self.database_path)
        await repository.initialize()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (99, 'future.sql', ?, '2026-08-08T00:00:00Z')
                """,
                ("f" * 64,),
            )
            connection.commit()
        finally:
            connection.close()

        restarted = SQLiteWorkflowRepository(self.database_path)
        with self.assertRaisesRegex(MigrationError, "workflow_schema_version_unknown"):
            await restarted.initialize()

    async def test_schema_pragma_mismatch_fails_closed(self) -> None:
        repository = SQLiteWorkflowRepository(self.database_path)
        await repository.initialize()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA user_version=1")
        finally:
            connection.close()

        restarted = SQLiteWorkflowRepository(self.database_path)
        with self.assertRaisesRegex(
            MigrationError, "workflow_schema_version_mismatch"
        ):
            await restarted.initialize()

    async def test_foreign_keys_and_state_constraints_are_enforced(self) -> None:
        repository = SQLiteWorkflowRepository(self.database_path)
        await repository.initialize()
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO source_messages(
                        draft_id, position, message_id, chat_id, sender_id,
                        text_content, media_kind, received_at
                    ) VALUES ('missing', 0, 1, 1, 1, '', 'text', '2026-08-08T00:00:00Z')
                    """
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO workflows(
                        draft_id, owner_id, chat_id, state, revision,
                        created_at, updated_at
                    ) VALUES (
                        'bad-state', 1, 1, 'legacy_review', 1,
                        '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
                    )
                    """
                )
        finally:
            connection.close()

    async def test_callback_schema_has_hash_only_and_no_raw_token_column(self) -> None:
        repository = SQLiteWorkflowRepository(self.database_path)
        await repository.initialize()
        connection = sqlite3.connect(self.database_path)
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(callback_tokens)")
            }
            create_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='callback_tokens'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertIn("token_hash", columns)
        self.assertNotIn("raw_token", columns)
        self.assertNotIn("opaque_token", columns)
        self.assertNotIn("raw_token", create_sql.casefold())
        self.assertNotIn("opaque_token", create_sql.casefold())

    async def test_synced_runtime_path_is_refused_before_creation(self) -> None:
        synced_path = (
            Path(self.temporary_directory.name)
            / "OneDrive"
            / "runtime"
            / "workflows.sqlite3"
        )
        repository = SQLiteWorkflowRepository(synced_path)

        with self.assertRaisesRegex(
            WorkflowRepositoryError, "workflow_database_must_be_local"
        ):
            await repository.initialize()
        self.assertFalse(synced_path.exists())

    def test_relative_paths_and_unbounded_busy_timeouts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SQLiteWorkflowRepository(Path("relative.sqlite3"))
        with self.assertRaises(ValueError):
            SQLiteWorkflowRepository(self.database_path, busy_timeout_seconds=0.01)
        with self.assertRaises(ValueError):
            SQLiteWorkflowRepository(self.database_path, busy_timeout_seconds=31)


if __name__ == "__main__":
    unittest.main()
