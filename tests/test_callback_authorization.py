"""Authorization matrix tests for CallbackService (P3-G)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Sequence

from dztgbot.domain.callbacks import CallbackAction, CallbackTokenRecord
from dztgbot.domain.fsm import DraftState
from dztgbot.domain.models import Draft
from dztgbot.domain.policy import DenialCode
from dztgbot.services.callback_service import CallbackService


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


class InMemoryDraftRepo:
    def __init__(self) -> None:
        self._drafts: dict[str, Draft] = {}

    async def save(self, draft: Draft) -> None:
        self._drafts[draft.draft_id] = draft

    async def get_by_id(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)

    async def compare_and_swap_state(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def record_attempt(self, attempt) -> None:  # pragma: no cover
        raise NotImplementedError

    async def update_attempt(self, attempt) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_latest_attempt(self, draft_id: str):  # pragma: no cover
        return None

    async def list_expired(self, before_utc: datetime) -> Sequence[Draft]:
        return []

    async def delete(self, draft_id: str) -> bool:
        return self._drafts.pop(draft_id, None) is not None


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._by_hash: dict[str, CallbackTokenRecord] = {}

    async def get_callback(self, token_hash: str) -> CallbackTokenRecord | None:
        return self._by_hash.get(token_hash)

    async def store_callback(self, record: CallbackTokenRecord) -> None:
        self._by_hash[record.token_hash] = record

    async def consume_callback(self, token_hash: str, consumed_at: datetime) -> bool:
        record = self._by_hash.get(token_hash)
        if record is None:
            return False
        if not record.one_shot:
            return True
        if record.consumed_at is not None:
            return False
        if consumed_at >= record.expires_at:
            return False
        self._by_hash[token_hash] = replace(record, consumed_at=consumed_at)
        return True

    async def invalidate_draft_preview_tokens(
        self, draft_id: str, *, at: datetime
    ) -> int:
        count = 0
        for token_hash, record in list(self._by_hash.items()):
            if record.draft_id != draft_id:
                continue
            # Expire immediately so authorize fails with TOKEN_EXPIRED.
            self._by_hash[token_hash] = replace(record, expires_at=at)
            count += 1
        return count


def _draft(
    *,
    draft_id: str = "draft-1",
    owner_id: int = 10,
    chat_id: int = 10,
    revision: int = 1,
    state: DraftState = DraftState.REVIEW,
    thread_id: int | None = None,
) -> Draft:
    now = datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc)
    return Draft(
        draft_id=draft_id,
        owner_id=owner_id,
        chat_id=chat_id,
        message_thread_id=thread_id,
        state=state,
        revision=revision,
        created_at=now,
        updated_at=now,
    )


class CallbackAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.drafts = InMemoryDraftRepo()
        self.tokens = InMemoryTokenStore()
        self.service = CallbackService(self.drafts, self.tokens, self.clock)
        self.draft = _draft()
        await self.drafts.save(self.draft)
        issued = await self.service.issue_preview_buttons(
            self.draft,
            actions=(CallbackAction.CONFIRM, CallbackAction.CANCEL, CallbackAction.TOGGLE_TYPE),
            preview_message_id=500,
        )
        self.confirm = issued[CallbackAction.CONFIRM]
        self.cancel = issued[CallbackAction.CANCEL]
        self.toggle = issued[CallbackAction.TOGGLE_TYPE]

    async def test_happy_path_confirm_allowed_and_consumes(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertTrue(result.allowed)
        self.assertIsNone(result.denial_code)
        self.assertEqual(result.action, CallbackAction.CONFIRM)
        self.assertIsNotNone(result.draft)
        assert result.draft is not None
        self.assertEqual(result.draft.draft_id, "draft-1")

        stored = await self.tokens.get_callback(self.confirm.token_hash)
        assert stored is not None
        self.assertIsNotNone(stored.consumed_at)

    async def test_malformed_callback_denied(self) -> None:
        result = await self.service.authorize(
            raw_callback_data="jira_confirm",
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.denial_code, DenialCode.MALFORMED_CALLBACK)

    async def test_group_chat_denied(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="group",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.NOT_PRIVATE_CHAT)

    async def test_foreign_actor_denied(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=99,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.FOREIGN_ACTOR)

    async def test_wrong_chat_denied(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=999,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.WRONG_CHAT)

    async def test_wrong_preview_message_denied(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=1,
        )
        self.assertEqual(result.denial_code, DenialCode.WRONG_MESSAGE)

    async def test_stale_revision_denied(self) -> None:
        # Bump draft revision without re-issuing tokens.
        bumped = replace(self.draft, revision=2, updated_at=self.clock.now())
        await self.drafts.save(bumped)
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.STALE_REVISION)

    async def test_illegal_state_and_already_processing(self) -> None:
        cancelled = replace(
            self.draft, state=DraftState.CANCELLED, updated_at=self.clock.now()
        )
        await self.drafts.save(cancelled)
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.ILLEGAL_STATE)

        submitting = replace(
            self.draft, state=DraftState.SUBMITTING, updated_at=self.clock.now()
        )
        await self.drafts.save(submitting)
        result2 = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result2.denial_code, DenialCode.ALREADY_PROCESSING)

    async def test_expired_token_denied(self) -> None:
        self.clock.advance(timedelta(hours=2))
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.TOKEN_EXPIRED)

    async def test_unknown_token_denied(self) -> None:
        # Valid grammar but never stored.
        from dztgbot.domain.callbacks import encode_callback_data, generate_opaque_token

        raw = encode_callback_data(CallbackAction.CONFIRM, generate_opaque_token())
        result = await self.service.authorize(
            raw_callback_data=raw,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertEqual(result.denial_code, DenialCode.UNKNOWN_TOKEN)

    async def test_toggle_is_not_consumed(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.toggle.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        self.assertTrue(result.allowed)
        stored = await self.tokens.get_callback(self.toggle.token_hash)
        assert stored is not None
        self.assertIsNone(stored.consumed_at)

    async def test_denial_user_message_is_non_leaky(self) -> None:
        result = await self.service.authorize(
            raw_callback_data=self.confirm.callback_data,
            actor_user_id=99,
            chat_id=10,
            chat_type="private",
            preview_message_id=500,
        )
        message = result.user_message
        self.assertIsNotNone(message)
        assert message is not None
        self.assertNotIn(self.confirm.callback_data, message)
        self.assertNotIn("j1:", message)

    async def test_wrong_thread_when_bound(self) -> None:
        threaded = _draft(draft_id="draft-t", thread_id=7)
        await self.drafts.save(threaded)
        issued = await self.service.issue_preview_buttons(
            threaded,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=600,
            message_thread_id=7,
        )
        button = issued[CallbackAction.CONFIRM]
        result = await self.service.authorize(
            raw_callback_data=button.callback_data,
            actor_user_id=10,
            chat_id=10,
            chat_type="private",
            message_thread_id=8,
            preview_message_id=600,
        )
        self.assertEqual(result.denial_code, DenialCode.WRONG_THREAD)

    async def test_no_telegram_imports_in_service_module(self) -> None:
        import ast
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "src" / "dztgbot" / "services" / "callback_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        for name in imports:
            self.assertFalse(
                name.startswith("telegram"),
                f"forbidden import: {name}",
            )


if __name__ == "__main__":
    unittest.main()
