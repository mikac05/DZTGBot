"""Application service for live Jira 8.4.1 issue triage, search, and actions.

Pure application logic interacting with JiraGateway and UserStore.
"""

from __future__ import annotations

import logging

from dztgbot.domain.models import JiraIssueView, JiraSearchResult, JiraTransitionView
from dztgbot.domain.ports import JiraGatewayPort
from dztgbot.user_store import UserStore


logger = logging.getLogger(__name__)


class JiraIssueService:
    """Coordinates search, retrieval, comments, transitions, assignments, and blocking for Jira issues."""

    def __init__(self, jira_gateway: JiraGatewayPort, user_store: UserStore) -> None:
        self._gateway: JiraGatewayPort = jira_gateway
        self._user_store = user_store

    def _get_pat(self, user_id: int) -> str:
        creds = self._user_store.get_credentials(user_id)
        if not creds or not creds.pat:
            raise ValueError("Authentication required. Use /auth to log in with your Jira PAT.")
        return str(creds.pat)

    async def get_issue(self, user_id: int, issue_key: str) -> JiraIssueView:
        pat = self._get_pat(user_id)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def search_my_open(self, user_id: int, max_results: int = 7) -> JiraSearchResult:
        pat = self._get_pat(user_id)
        jql = "assignee = currentUser() AND resolution = Unresolved ORDER BY priority DESC, updated DESC"
        return await self._gateway.search_jql(pat, jql, max_results=max_results)

    async def search_i_created(self, user_id: int, max_results: int = 7) -> JiraSearchResult:
        pat = self._get_pat(user_id)
        jql = "reporter = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
        return await self._gateway.search_jql(pat, jql, max_results=max_results)

    async def search_unassigned(self, user_id: int, max_results: int = 7) -> JiraSearchResult:
        pat = self._get_pat(user_id)
        jql = "assignee is EMPTY AND resolution = Unresolved ORDER BY updated DESC"
        return await self._gateway.search_jql(pat, jql, max_results=max_results)

    async def search_blocked(self, user_id: int, max_results: int = 7) -> JiraSearchResult:
        pat = self._get_pat(user_id)
        jql = "(Flagged = Impediment OR issueFunction in linkedIssuesOf('resolution = Unresolved', 'is blocked by')) AND resolution = Unresolved ORDER BY updated DESC"
        return await self._gateway.search_jql(pat, jql, max_results=max_results)

    async def search_sprint(self, user_id: int, max_results: int = 7) -> JiraSearchResult:
        pat = self._get_pat(user_id)
        jql = "sprint in openSprints() AND (assignee = currentUser() OR reporter = currentUser()) AND resolution = Unresolved ORDER BY priority DESC"
        return await self._gateway.search_jql(pat, jql, max_results=max_results)

    async def search_keywords(self, user_id: int, keywords: str, max_results: int = 7) -> JiraSearchResult:
        pat = self._get_pat(user_id)
        safe_kw = keywords.replace('"', '\\"')
        jql = f'text ~ "{safe_kw}" AND resolution = Unresolved ORDER BY updated DESC'
        return await self._gateway.search_jql(pat, jql, max_results=max_results)

    async def get_transitions(self, user_id: int, issue_key: str) -> tuple[JiraTransitionView, ...]:
        pat = self._get_pat(user_id)
        return await self._gateway.get_transitions(issue_key, pat)

    async def execute_transition(
        self, user_id: int, issue_key: str, transition_id: str, comment: str | None = None
    ) -> JiraIssueView:
        pat = self._get_pat(user_id)
        audit_note = f"via Telegram by user {user_id}"
        if comment:
            audit_note = f"{comment}\n\n({audit_note})"
        await self._gateway.execute_transition(issue_key, transition_id, pat, comment=audit_note)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def add_comment(self, user_id: int, issue_key: str, comment_text: str) -> JiraIssueView:
        pat = self._get_pat(user_id)
        body = f"{comment_text}\n\n*(via Telegram)*"
        await self._gateway.add_comment(issue_key, body, pat)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def upload_attachment(
        self, user_id: int, issue_key: str, filename: str, content: bytes, mime_type: str
    ) -> JiraIssueView:
        pat = self._get_pat(user_id)
        await self._gateway.upload_attachment(issue_key, filename, content, mime_type, pat)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def assign_issue(self, user_id: int, issue_key: str, assignee_name: str) -> JiraIssueView:
        pat = self._get_pat(user_id)
        await self._gateway.assign_issue(issue_key, assignee_name, pat)
        await self._gateway.add_comment(issue_key, f"Reassigned to {assignee_name if assignee_name else 'Unassigned'} via Telegram.", pat)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def block_issue(
        self, user_id: int, issue_key: str, blocker_key: str, reason: str | None = None
    ) -> JiraIssueView:
        pat = self._get_pat(user_id)
        await self._gateway.block_issue(issue_key, blocker_key, pat, reason=reason)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def unblock_issue(self, user_id: int, issue_key: str, link_id: str) -> JiraIssueView:
        pat = self._get_pat(user_id)
        await self._gateway.unblock_issue(issue_key, link_id, pat)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def watch_issue(self, user_id: int, issue_key: str) -> JiraIssueView:
        pat = self._get_pat(user_id)
        await self._gateway.watch_issue(issue_key, pat)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def unwatch_issue(self, user_id: int, issue_key: str) -> JiraIssueView:
        pat = self._get_pat(user_id)
        await self._gateway.unwatch_issue(issue_key, pat)
        return await self._gateway.get_issue_details(issue_key, pat)

    async def get_standup_summary(
        self, user_id: int
    ) -> tuple[
        tuple[JiraIssueView, ...],
        tuple[JiraIssueView, ...],
        tuple[JiraIssueView, ...],
        tuple[JiraIssueView, ...],
    ]:
        """Fetch active sprint / open tickets and group into Blocked, In Progress, Testing/QA, and Done."""
        pat = self._get_pat(user_id)
        jql = "sprint in openSprints() OR (assignee = currentUser() AND updated >= -7d) ORDER BY priority DESC, updated DESC"
        search_res = await self._gateway.search_jql(pat, jql, max_results=30)

        blocked: list[JiraIssueView] = []
        in_progress: list[JiraIssueView] = []
        in_qa: list[JiraIssueView] = []
        done: list[JiraIssueView] = []

        for issue in search_res.issues:
            st = issue.status.lower()
            if issue.is_flagged or issue.blocker_keys:
                blocked.append(issue)
            elif "done" in st or "closed" in st or "resolved" in st:
                done.append(issue)
            elif "qa" in st or "testing" in st or "review" in st or "待測" in st:
                in_qa.append(issue)
            else:
                in_progress.append(issue)

        return tuple(blocked), tuple(in_progress), tuple(in_qa), tuple(done)
