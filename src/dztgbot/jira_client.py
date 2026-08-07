"""Async Jira Server REST API v2 client for credential validation and issue creation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .analysis import JiraTaskTemplate

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JiraUser:
    """Identity returned by the Jira Server /myself endpoint."""

    username: str
    display_name: str
    email: str | None


@dataclass(frozen=True, slots=True)
class CreatedIssue:
    """Result of a successful Jira issue creation."""

    key: str
    url: str


class JiraClientError(RuntimeError):
    """Raised when a Jira API call fails."""


class JiraClient:
    """Non-blocking Jira Server / Data Center REST API v2 client.

    Each API call uses a per-user Personal Access Token (Bearer auth)
    and is scoped to a single httpx session to avoid connection leaks.
    """

    def __init__(
        self,
        *,
        base_url: str,
        verify_ssl: bool = True,
        timeout_seconds: float = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_url = f"{self._base_url}/rest/api/2"
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds

    async def validate_credentials(self, pat: str) -> JiraUser:
        """Validate a PAT by calling /rest/api/2/myself.  Returns the user identity."""

        async with self._make_client(pat) as client:
            try:
                response = await client.get(f"{self._api_url}/myself")
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 401:
                    raise JiraClientError(
                        "Authentication failed. Check your Personal Access Token."
                    ) from error
                if error.response.status_code == 403:
                    raise JiraClientError(
                        "Access denied. Your token may lack the required permissions."
                    ) from error
                raise JiraClientError(
                    f"Jira returned HTTP {error.response.status_code}."
                ) from error
            except httpx.ConnectError as error:
                raise JiraClientError(
                    "Could not connect to the Jira server. "
                    "Check VPN connectivity and server availability."
                ) from error
            except httpx.TimeoutException as error:
                raise JiraClientError("Connection to Jira timed out.") from error
            except httpx.HTTPError as error:
                raise JiraClientError("Jira request failed.") from error

            data = response.json()
            return JiraUser(
                username=data.get("name", ""),
                display_name=data.get("displayName", ""),
                email=data.get("emailAddress"),
            )

    async def create_issue(
        self, pat: str, template: JiraTaskTemplate
    ) -> CreatedIssue:
        """Create a Jira issue from a validated template.  Returns the created issue key and URL."""

        fields: dict[str, object] = {
            "summary": template.summary[:255],
            "description": template.description,
            "issuetype": {"name": template.issuetype},
            "priority": {"name": template.priority},
        }
        if template.project_key:
            fields["project"] = {"key": template.project_key}
        if template.labels:
            fields["labels"] = template.labels
        if template.components:
            fields["components"] = [{"name": c} for c in template.components]
        if template.assignee:
            fields["assignee"] = {"name": template.assignee}

        # Jira REST API v2 has no native acceptance-criteria field;
        # append them to the description so they are visible on the issue.
        if template.acceptance_criteria:
            criteria_text = "\n\nAcceptance Criteria:\n" + "\n".join(
                f"* {criterion}" for criterion in template.acceptance_criteria
            )
            fields["description"] = (fields["description"] or "") + criteria_text

        payload = {"fields": fields}

        async with self._make_client(pat) as client:
            try:
                response = await client.post(
                    f"{self._api_url}/issue", json=payload
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                detail = self._extract_jira_error(error)
                if status == 401:
                    raise JiraClientError(
                        "Authentication expired. Use /auth to reconnect."
                    ) from error
                if status == 400 and detail:
                    raise JiraClientError(
                        f"Jira rejected the issue: {detail}"
                    ) from error
                raise JiraClientError(
                    f"Jira returned HTTP {status}."
                    + (f" {detail}" if detail else "")
                ) from error
            except httpx.ConnectError as error:
                raise JiraClientError(
                    "Could not connect to Jira. Check VPN connectivity."
                ) from error
            except httpx.TimeoutException as error:
                raise JiraClientError("Jira request timed out.") from error
            except httpx.HTTPError as error:
                raise JiraClientError("Jira request failed.") from error

            data = response.json()
            issue_key = data["key"]
            issue_url = f"{self._base_url}/browse/{issue_key}"
            LOGGER.info("Created Jira issue %s", issue_key)
            return CreatedIssue(key=issue_key, url=issue_url)

    def _make_client(self, pat: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            verify=self._verify_ssl,
            timeout=self._timeout_seconds,
        )

    @staticmethod
    def _extract_jira_error(error: httpx.HTTPStatusError) -> str:
        """Best-effort extraction of human-readable Jira error messages."""

        try:
            body = error.response.json()
            messages = body.get("errorMessages", [])
            errors = body.get("errors", {})
            parts = list(messages) + [f"{k}: {v}" for k, v in errors.items()]
            return "; ".join(parts) if parts else ""
        except Exception:
            return ""
