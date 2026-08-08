from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Attachment, Draft, JiraTaskTemplate, PublishedIssue
from dztgbot.infrastructure.persistence.workflow_sqlite import AttachmentStatus, SQLiteWorkflowRepository
from dztgbot.services.attachment_service import AttachmentContent, AttachmentPolicy, AttachmentService


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


class Loader:
    def __init__(self) -> None:
        self.fail = True

    async def load(self, file_id: str) -> AttachmentContent:
        if self.fail:
            raise RuntimeError("private provider detail")
        return AttachmentContent(b"image", "photo.jpg", "image/jpeg")


class Gateway:
    def __init__(self) -> None:
        self.uploads = 0

    async def upload_attachment(self, issue_key, filename, content, mime_type, pat):
        self.uploads += 1
        return "attachment-1"


class AttachmentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteWorkflowRepository(Path(self.temp.name) / "db.sqlite", enable_wal=False)
        await self.repo.initialize()
        self.loader, self.gateway = Loader(), Gateway()
        self.service = AttachmentService(self.repo, self.gateway, self.loader)
        draft = Draft(
            "draft", 1, 1, state=DraftState.CREATED, revision=2,
            template=JiraTaskTemplate("BOT", "Task", "s", "d", "Medium"),
            attachments=(Attachment("file", "unique", file_name="photo.jpg", file_size=5),),
            created_at=NOW, updated_at=NOW,
            published_issue=PublishedIssue("BOT-1", "1", "https://jira.invalid/browse/BOT-1", NOW),
        )
        await self.repo.save(draft)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_failed_attachment_retries_without_issue_recreation(self) -> None:
        first = await self.service.upload_pending("draft", "pat")
        self.assertEqual(first.draft.state, DraftState.ATTACHMENT_PARTIAL)
        self.assertEqual((await self.repo.list_attachments("draft"))[0].status, AttachmentStatus.FAILED)
        self.loader.fail = False
        second = await self.service.upload_pending("draft", "pat")
        self.assertEqual(second.draft.state, DraftState.COMPLETE)
        self.assertEqual(self.gateway.uploads, 1)
        self.assertFalse(hasattr(self.gateway, "create_issue"))

    async def test_declared_oversize_is_skipped_before_download(self) -> None:
        current = await self.repo.get_by_id("draft")
        assert current is not None
        oversized = replace(
            current,
            revision=current.revision + 1,
            attachments=(replace(current.attachments[0], file_size=6),),
        )
        await self.repo.save(oversized)
        service = AttachmentService(self.repo, self.gateway, self.loader, policy=AttachmentPolicy(max_file_bytes=5, max_total_bytes=5))
        result = await service.upload_pending("draft", "pat")
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.draft.state, DraftState.COMPLETE)


if __name__ == "__main__":
    unittest.main()
