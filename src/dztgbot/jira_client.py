"""Async Jira Server REST API v2 client for credential validation and issue creation."""

from __future__ import annotations

import base64
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
        vpn_manager: object | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_url = f"{self._base_url}/rest/api/2"
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds
        self._vpn_manager = vpn_manager

    async def _ensure_vpn(self) -> None:
        if self._vpn_manager is not None and hasattr(self._vpn_manager, "start"):
            await self._vpn_manager.start()

    async def validate_credentials(self, pat: str) -> JiraUser:
        """Validate a PAT by calling /rest/api/2/myself.  Returns the user identity."""

        await self._ensure_vpn()
        async with self._make_client(pat) as client:
            try:
                response = await client.get(f"{self._api_url}/myself")
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 401:
                    raise JiraClientError(
                        "認證失敗，請檢查您的個人存取令牌。"
                    ) from error
                if error.response.status_code == 403:
                    raise JiraClientError(
                        "存取被拒，您的令牌可能缺少必要權限。"
                    ) from error
                raise JiraClientError(
                    f"Jira 伺服器回傳 HTTP {error.response.status_code}。"
                ) from error
            except httpx.ConnectError as error:
                raise JiraClientError(
                    "無法連線至 Jira 伺服器，請檢查 VPN 連線與伺服器狀態。"
                ) from error
            except httpx.TimeoutException as error:
                raise JiraClientError("Jira 連線逾時。") from error
            except httpx.HTTPError as error:
                raise JiraClientError("Jira 請求失敗。") from error

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
            criteria_text = "\n\n驗收標準:\n" + "\n".join(
                f"* {criterion}" for criterion in template.acceptance_criteria
            )
            fields["description"] = (fields["description"] or "") + criteria_text

        payload = {"fields": fields}

        await self._ensure_vpn()
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
                        "認證已過期，請使用 /auth 重新綁定。"
                    ) from error
                if status == 400 and "issuetype" in detail.lower() and fields.get("issuetype", {}).get("name") != "Task":
                    LOGGER.warning("Jira rejected issuetype '%s', retrying with 'Task'", fields.get("issuetype", {}).get("name"))
                    fields["issuetype"] = {"name": "Task"}
                    try:
                        retry_resp = await client.post(f"{self._api_url}/issue", json={"fields": fields})
                        retry_resp.raise_for_status()
                        response = retry_resp
                    except Exception:
                        raise JiraClientError(f"Jira 拒絕建立工單: {detail}") from error
                elif status == 400 and detail:
                    raise JiraClientError(
                        f"Jira 拒絕建立工單: {detail}"
                    ) from error
                else:
                    raise JiraClientError(
                        f"Jira returned HTTP {status}."
                        + (f" {detail}" if detail else "")
                    ) from error
            except httpx.ConnectError as error:
                raise JiraClientError(
                    "無法連線至 Jira，請檢查 VPN 連線。"
                ) from error
            except httpx.TimeoutException as error:
                raise JiraClientError("Jira 請求逾時。") from error
            except httpx.HTTPError as error:
                raise JiraClientError("Jira 請求失敗。") from error

            data = response.json()
            issue_key = data["key"]
            issue_url = f"{self._base_url}/browse/{issue_key}"
            LOGGER.info("Created Jira issue %s", issue_key)
            return CreatedIssue(key=issue_key, url=issue_url)

    async def update_issue(
        self, pat: str, issue_key: str, template: JiraTaskTemplate
    ) -> CreatedIssue:
        """Update fields of an existing Jira issue."""

        fields: dict[str, object] = {
            "summary": template.summary[:255],
            "description": template.description,
            "issuetype": {"name": template.issuetype},
            "priority": {"name": template.priority},
        }
        payload = {"fields": fields}

        await self._ensure_vpn()
        async with self._make_client(pat) as client:
            try:
                response = await client.put(
                    f"{self._api_url}/issue/{issue_key}", json=payload
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                detail = self._extract_jira_error(error)
                if status == 401:
                    raise JiraClientError(
                        "認證已過期，請使用 /auth 重新綁定。"
                    ) from error
                if status == 400 and "issuetype" in detail.lower() and fields.get("issuetype", {}).get("name") != "Task":
                    LOGGER.warning("Jira rejected issuetype '%s', retrying update with 'Task'", fields.get("issuetype", {}).get("name"))
                    fields["issuetype"] = {"name": "Task"}
                    try:
                        retry_resp = await client.put(f"{self._api_url}/issue/{issue_key}", json={"fields": fields})
                        retry_resp.raise_for_status()
                        response = retry_resp
                    except Exception:
                        raise JiraClientError(f"Jira 拒絕更新工單: {detail}") from error
                elif status == 400 and detail:
                    raise JiraClientError(f"Jira 拒絕更新工單: {detail}") from error
                else:
                    raise JiraClientError(
                        f"Jira returned HTTP {status}."
                        + (f" {detail}" if detail else "")
                    ) from error
            except httpx.ConnectError as error:
                raise JiraClientError(
                    "無法連線至 Jira，請檢查 VPN 連線。"
                ) from error
            except httpx.TimeoutException as error:
                raise JiraClientError("Jira 請求逾時。") from error
            except httpx.HTTPError as error:
                raise JiraClientError("Jira 請求失敗。") from error

            issue_url = f"{self._base_url}/browse/{issue_key}"
            LOGGER.info("Updated Jira issue %s", issue_key)
            return CreatedIssue(key=issue_key, url=issue_url)

    async def add_attachment(
        self,
        pat: str,
        issue_key: str,
        filename: str,
        content: bytes,
        mime_type: str = "image/jpeg",
    ) -> None:
        """Upload a file attachment to an existing Jira issue."""

        await self._ensure_vpn()
        async with self._make_client(pat) as client:
            headers = {"X-Atlassian-Token": "no-check"}
            files = {"file": (filename, content, mime_type)}
            try:
                response = await client.post(
                    f"{self._api_url}/issue/{issue_key}/attachments",
                    headers=headers,
                    files=files,
                )
                response.raise_for_status()
                LOGGER.info("Successfully attached %s to Jira issue %s", filename, issue_key)
            except httpx.HTTPStatusError as error:
                detail = self._extract_jira_error(error)
                LOGGER.error(
                    "Failed to attach %s to Jira issue %s: HTTP %s (%s)",
                    filename,
                    issue_key,
                    error.response.status_code,
                    detail,
                )
                raise JiraClientError(f"附件上傳失敗: HTTP {error.response.status_code}") from error
            except Exception as error:
                LOGGER.error("Attachment upload error for %s (%s)", filename, type(error).__name__)
                raise JiraClientError("附件上傳因網路錯誤而失敗。") from error

    def _make_client(self, token_or_auth: str) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/json",
        }
        raw = token_or_auth.strip()
        if raw.startswith("Basic ") or raw.startswith("Bearer "):
            headers["Authorization"] = raw
        elif ":" in raw and not raw.startswith("http"):
            encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        elif "JSESSIONID=" in raw or raw.isalnum() and len(raw) > 20 and not raw.startswith("ATATT"):
            if "JSESSIONID=" in raw:
                headers["Cookie"] = raw if raw.startswith("JSESSIONID=") else f"JSESSIONID={raw}"
            else:
                headers["Authorization"] = f"Bearer {raw}"
        else:
            headers["Authorization"] = f"Bearer {raw}"

        return httpx.AsyncClient(
            headers=headers,
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
