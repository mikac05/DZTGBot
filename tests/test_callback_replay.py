"""Replay, race, and preview invalidation tests for CallbackService (P3-G)."""

from __future__ import annotations

import asyncio
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
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)

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
    """Token store with a lock to exercise concurrent consume races."""

    def __init__(self) -> None:
        self._by_hash: dict[str, CallbackTokenRecord] = {}
        self._lock = asyncio.Lock()

    async def get_callback(self, token_hash: str) -> CallbackTokenRecord | None:
        async with self._lock:
            return self._by_hash.get(token_hash)

    async def store_callback(self, record: CallbackTokenRecord) -> None:
        async with self._lock:
            self._by_hash[record.token_hash] = record

    async def consume_callback(self, token_hash: str, consumed_at: datetime) -> bool:
        async with self._lock:
            record = self._by_hash.get(token_hash)
            if record is None:
                return False
            if not record.one_shot:
                return True
            if record.consumed_at is not None:
                return False
            if consumed_at >= record.expires_at:
                return False
            # Yield to allow interleaving in concurrent tests.
            await asyncio.sleep(0)
            # Re-check after yield.
            record = self._by_hash.get(token_hash)
            if record is None or record.consumed_at is not None:
                return False
            self._by_hash[token_hash] = replace(record, consumed_at=consumed_at)
            return True

    async def invalidate_draft_preview_tokens(
        self, draft_id: str, *, at: datetime
    ) -> int:
        async with self._lock:
            count = 0
            for token_hash, record in list(self._by_hash.items()):
                if record.draft_id != draft_id:
                    continue
                self._by_hash[token_hash] = replace(record, expires_at=at)
                count += 1
            return count


def _draft(
    *,
    draft_id: str = "draft-r",
    revision: int = 1,
    state: DraftState = DraftState.REVIEW,
) -> Draft:
    now = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    return Draft(
        draft_id=draft_id,
        owner_id=42,
        chat_id=42,
        message_thread_id=None,
        state=state,
        revision=revision,
        created_at=now,
        updated_at=now,
    )


class CallbackReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.drafts = InMemoryDraftRepo()
        self.tokens = InMemoryTokenStore()
        self.service = CallbackService(self.drafts, self.tokens, self.clock)
        self.draft = _draft()
        await self.drafts.save(self.draft)
        issued = await self.service.issue_preview_buttons(
            self.draft,
            actions=(CallbackAction.CONFIRM, CallbackAction.CANCEL),
            preview_message_id=700,
        )
        self.confirm_data = issued[CallbackAction.CONFIRM].callback_data
        self.confirm_hash = issued[CallbackAction.CONFIRM].token_hash

    async def _authorize(self, raw: str) -> object:
        return await self.service.authorize(
            raw_callback_data=raw,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=700,
        )

    async def test_second_click_same_token_is_consumed(self) -> None:
        first = await self._authorize(self.confirm_data)
        self.assertTrue(first.allowed)  # type: ignore[attr-defined]

        second = await self._authorize(self.confirm_data)
        self.assertFalse(second.allowed)  # type: ignore[attr-defined]
        self.assertEqual(second.denial_code, DenialCode.TOKEN_CONSUMED)  # type: ignore[attr-defined]

    async def test_copied_token_in_same_chat_cannot_mutate_twice(self) -> None:
        """Even the legitimate owner replaying a copied button is blocked."""
        await self._authorize(self.confirm_data)
        copy_replay = await self.service.authorize(
            raw_callback_data=self.confirm_data,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=700,
        )
        self.assertEqual(copy_replay.denial_code, DenialCode.TOKEN_CONSUMED)

    async def test_concurrent_double_confirm_only_one_wins(self) -> None:
        results = await asyncio.gather(
            self._authorize(self.confirm_data),
            self._authorize(self.confirm_data),
        )
        allowed = [r for r in results if r.allowed]  # type: ignore[attr-defined]
        denied = [r for r in results if not r.allowed]  # type: ignore[attr-defined]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].denial_code, DenialCode.TOKEN_CONSUMED)  # type: ignore[attr-defined]

    async def test_new_preview_revision_invalidates_old_buttons(self) -> None:
        old_data = self.confirm_data

        # Simulate a new preview revision and re-issue buttons.
        new_draft = replace(self.draft, revision=2, updated_at=self.clock.now())
        await self.drafts.save(new_draft)
        await self.service.on_preview_revision_committed(new_draft.draft_id)
        new_issued = await self.service.issue_preview_buttons(
            new_draft,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=701,
            invalidate_previous=False,  # already invalidated above
        )
        new_data = new_issued[CallbackAction.CONFIRM].callback_data

        old_result = await self.service.authorize(
            raw_callback_data=old_data,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=700,
        )
        self.assertEqual(old_result.denial_code, DenialCode.TOKEN_EXPIRED)

        new_result = await self.service.authorize(
            raw_callback_data=new_data,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=701,
        )
        self.assertTrue(new_result.allowed)

    async def test_issue_preview_invalidates_previous_by_default(self) -> None:
        first = self.confirm_data
        revised = replace(self.draft, revision=3, updated_at=self.clock.now())
        await self.drafts.save(revised)
        second_issue = await self.service.issue_preview_buttons(
            revised,
            actions=(CallbackAction.CONFIRM,),
            preview_message_id=800,
        )
        # Old confirm must fail (expired via invalidation).
        old = await self.service.authorize(
            raw_callback_data=first,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=700,
        )
        self.assertEqual(old.denial_code, DenialCode.TOKEN_EXPIRED)

        new = await self.service.authorize(
            raw_callback_data=second_issue[CallbackAction.CONFIRM].callback_data,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=800,
        )
        self.assertTrue(new.allowed)

    async def test_cancel_is_one_shot(self) -> None:
        issued = await self.service.issue_preview_buttons(
            self.draft,
            actions=(CallbackAction.CANCEL,),
            preview_message_id=900,
        )
        raw = issued[CallbackAction.CANCEL].callback_data
        first = await self.service.authorize(
            raw_callback_data=raw,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=900,
        )
        second = await self.service.authorize(
            raw_callback_data=raw,
            actor_user_id=42,
            chat_id=42,
            chat_type="private",
            preview_message_id=900,
        )
        self.assertTrue(first.allowed)
        self.assertEqual(second.denial_code, DenialCode.TOKEN_CONSUMED)


if __name__ == "__main__":
    unittest.main()
