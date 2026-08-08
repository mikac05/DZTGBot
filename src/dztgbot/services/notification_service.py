"""Background notification polling service for Jira Server updates.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from dztgbot.domain.models import JiraIssueView
from dztgbot.services.jira_issue_service import JiraIssueService
from dztgbot.user_store import UserStore


logger = logging.getLogger(__name__)

NotifierFunc = Callable[[int, JiraIssueView], Awaitable[None]]


class NotificationPollerService:
    """Background service polling Jira Server unread/recent updates for users."""

    def __init__(
        self,
        jira_issue_service: JiraIssueService,
        user_store: UserStore,
        workflow_repo: Any,
        notifier_func: NotifierFunc | None = None,
    ) -> None:
        self._issue_service = jira_issue_service
        self._user_store = user_store
        self._repo = workflow_repo
        self._notifier_func = notifier_func

    async def poll_user_notifications(self, user_id: int) -> None:
        """Poll recent Jira updates for a specific user and send Telegram push alerts."""
        try:
            res = await self._issue_service.search_my_open(user_id)
            for issue in res.issues:
                last_notified = await self._repo.get_last_notified_update(user_id, issue.issue_key)
                if last_notified is None or last_notified != issue.last_comment_summary:
                    await self._repo.record_notification(user_id, issue.issue_key, issue.last_comment_summary)
                    if last_notified is not None and self._notifier_func is not None:
                        await self._notifier_func(user_id, issue)
        except Exception as error:
            logger.debug("Notification poll skipped for user %s: %s", user_id, error)
