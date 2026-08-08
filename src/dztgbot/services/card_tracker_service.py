"""Application service for tracking and updating Telegram Issue Cards across chats.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from dztgbot.domain.models import JiraIssueView


logger = logging.getLogger(__name__)


class CardTrackerService:
    """Tracks message references for posted issue cards and triggers cross-chat updates."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def register_card(self, chat_id: int, message_id: int, issue_key: str, owner_id: int) -> None:
        """Register a posted issue card message."""
        await self._repository.register_card_message(chat_id, message_id, issue_key, owner_id)

    async def refresh_issue_cards(
        self,
        issue_view: JiraIssueView,
        updater: Callable[[int, int, JiraIssueView], Awaitable[None]],
    ) -> None:
        """Fetch all tracked messages for an issue key and trigger the updater callback."""
        records = await self._repository.get_card_messages_for_issue(issue_view.issue_key)
        for chat_id, message_id, _owner_id in records:
            try:
                await updater(chat_id, message_id, issue_view)
            except Exception as error:
                logger.warning(
                    "Failed to refresh card message chat_id=%s message_id=%s issue_key=%s: %s",
                    chat_id,
                    message_id,
                    issue_view.issue_key,
                    error,
                )
