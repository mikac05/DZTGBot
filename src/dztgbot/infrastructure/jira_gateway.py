"""Bounded Jira Server/Data Center REST v2 adapter.

The adapter owns one connection pool for its lifecycle.  Authentication is
deliberately supplied on every request and is never installed on the client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx

from dztgbot.domain.errors import (
    ClassifiedOperationError,
    ErrorClassification,
    ErrorKind,
    MutationCertainty,
    Operation,
    Retryability,
    SafeErrorCode,
    classify_definite_mutation_failure,
    classify_unknown_mutation_outcome,
)
from dztgbot.domain.models import JiraTaskTemplate, PublishedIssue


MAX_ERROR_BODY_BYTES = 16_384
DEFAULT_METADATA_TTL_SECONDS = 60.0
DEFAULT_SAFE_RETRIES = 2


@dataclass(frozen=True, slots=True)
class JiraTimeouts:
    connect: float = 5.0
    read: float = 20.0
    write: float = 20.0
    pool: float = 5.0

    def __post_init__(self) -> None:
        if min(self.connect, self.read, self.write, self.pool) <= 0:
            raise ValueError("Jira timeouts must be positive")

    def as_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect, read=self.read, write=self.write, pool=self.pool
        )


@dataclass(frozen=True, slots=True)
class JiraIssueFields:
    """Canonical Jira field map shared by create, update, hash, and diff."""

    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping) or not self.fields:
            raise ValueError("fields must be a non-empty mapping")

    def as_dict(self) -> dict[str, object]:
        return dict(self.fields)


@dataclass(frozen=True, slots=True)
class JiraMetadata:
    project_key: str
    issue_types: tuple[str, ...]
    priorities: tuple[str, ...]
    marker_supported: bool = False

    def __post_init__(self) -> None:
        if not self.project_key or not self.issue_types:
            raise ValueError("Jira metadata requires project and issue types")


@dataclass(frozen=True, slots=True)
class JiraRemoteIssue:
    issue_key: str
    issue_id: str
    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.issue_key or not self.issue_id or not isinstance(self.fields, Mapping):
            raise ValueError("invalid Jira issue response")


class JiraGatewayError(ClassifiedOperationError):
    """A provider failure containing only bounded, safe metadata."""

    def __init__(
        self,
        classification: ErrorClassification,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        field_codes: tuple[str, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.field_codes = field_codes
        super().__init__(classification)


def canonical_issue_fields(template: JiraTaskTemplate) -> JiraIssueFields:
    """Map a reviewed template without substitution or truncation."""

    if not template.project_key.strip():
        raise ValueError("project_key must not be empty")
    if not template.issue_type.strip():
        raise ValueError("issue_type must not be empty")
    if not template.summary.strip():
        raise ValueError("summary must not be empty")
    if not template.description.strip():
        raise ValueError("description must not be empty")
    if not template.priority.strip():
        raise ValueError("priority must not be empty")

    description = template.description
    if template.acceptance_criteria:
        description += "\n\nAcceptance Criteria:\n" + "\n".join(
            f"* {criterion}" for criterion in template.acceptance_criteria
        )
    fields: dict[str, object] = {
        "project": {"key": template.project_key},
        "issuetype": {"name": template.issue_type},
        "summary": template.summary,
        "description": description,
        "priority": {"name": template.priority},
        "labels": list(template.labels),
        "components": [{"name": component} for component in template.components],
    }
    if template.assignee:
        fields["assignee"] = {"name": template.assignee}
    return JiraIssueFields(fields)


def canonical_request_hash(template: JiraTaskTemplate) -> str:
    encoded = json.dumps(
        canonical_issue_fields(template).as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def diff_issue_fields(
    before: JiraTaskTemplate, after: JiraTaskTemplate
) -> dict[str, object]:
    old = canonical_issue_fields(before).as_dict()
    new = canonical_issue_fields(after).as_dict()
    return {key: value for key, value in new.items() if old.get(key) != value}


class JiraGateway:
    """Lifecycle-managed Jira adapter with request-local PAT isolation."""

    def __init__(
        self,
        *,
        base_url: str,
        verify: bool | str = True,
        timeouts: JiraTimeouts | None = None,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        metadata_ttl_seconds: float = DEFAULT_METADATA_TTL_SECONDS,
        safe_retries: int = DEFAULT_SAFE_RETRIES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if metadata_ttl_seconds <= 0 or safe_retries < 0:
            raise ValueError("cache TTL and retry count are invalid")
        self._base_url = base_url.rstrip("/")
        self._api_url = f"{self._base_url}/rest/api/2"
        self._timeouts = timeouts or JiraTimeouts()
        self._metadata_ttl = metadata_ttl_seconds
        self._safe_retries = safe_retries
        self._metadata_cache: dict[tuple[str, str], tuple[float, JiraMetadata]] = {}
        self._cache_lock = asyncio.Lock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=verify,
            timeout=self._timeouts.as_httpx(),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
            headers={"Accept": "application/json"},
        )
        self._closed = False

    async def __aenter__(self) -> "JiraGateway":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed and self._owns_client:
            await self._client.aclose()
        self._closed = True

    @staticmethod
    def _headers(pat: str, *, attachment: bool = False) -> dict[str, str]:
        token = pat.strip()
        if not token or token.lower().startswith(("basic ", "bearer ")):
            raise ValueError("pat must be a raw Personal Access Token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if attachment:
            headers["X-Atlassian-Token"] = "no-check"
        return headers

    async def test_credential(self, pat: str) -> bool:
        try:
            response = await self._safe_request("GET", "/myself", pat, Operation.AUTHENTICATION)
            data = self._json_object(response, Operation.AUTHENTICATION, required=("name",))
            return bool(data["name"])
        except JiraGatewayError as error:
            if error.classification.kind in {ErrorKind.AUTHENTICATION, ErrorKind.PERMISSION}:
                return False
            raise

    async def create_issue(
        self,
        template: JiraTaskTemplate,
        pat: str,
        idempotency_key: str | None = None,
    ) -> PublishedIssue:
        payload: dict[str, object] = {"fields": canonical_issue_fields(template).as_dict()}
        if idempotency_key:
            payload["properties"] = [
                {"key": "dztgbot.request_hash", "value": idempotency_key}
            ]
        response = await self._mutation_request(
            "POST", "/issue", pat, Operation.JIRA_CREATE, json_payload=payload
        )
        data = self._json_object(response, Operation.JIRA_CREATE, required=("id", "key"))
        issue_id, issue_key = data["id"], data["key"]
        if not isinstance(issue_id, str) or not isinstance(issue_key, str):
            raise self._contract_error(Operation.JIRA_CREATE)
        return PublishedIssue(
            issue_key=issue_key,
            issue_id=issue_id,
            issue_url=f"{self._base_url}/browse/{quote(issue_key, safe='-')}",
            published_at=datetime.now(timezone.utc),
        )

    async def update_issue(
        self, issue_key: str, template: JiraTaskTemplate, pat: str
    ) -> None:
        await self.update_issue_fields(
            issue_key, canonical_issue_fields(template).as_dict(), pat
        )

    async def update_issue_fields(
        self, issue_key: str, fields: Mapping[str, object], pat: str
    ) -> None:
        if not fields:
            return
        await self._mutation_request(
            "PUT",
            f"/issue/{quote(issue_key, safe='-')}",
            pat,
            Operation.JIRA_UPDATE,
            json_payload={"fields": dict(fields)},
        )

    async def upload_attachment(
        self,
        issue_key: str,
        filename: str,
        content: bytes,
        mime_type: str,
        pat: str,
    ) -> str:
        if not filename or not content or not mime_type:
            raise ValueError("attachment fields must not be empty")
        response = await self._mutation_request(
            "POST",
            f"/issue/{quote(issue_key, safe='-')}/attachments",
            pat,
            Operation.JIRA_ATTACHMENT,
            files={"file": (filename, content, mime_type)},
            attachment=True,
        )
        data = self._json_array(response, Operation.JIRA_ATTACHMENT)
        if len(data) != 1 or not isinstance(data[0], Mapping):
            raise self._contract_error(Operation.JIRA_ATTACHMENT)
        attachment_id = data[0].get("id")
        if not isinstance(attachment_id, str) or not attachment_id:
            raise self._contract_error(Operation.JIRA_ATTACHMENT)
        return attachment_id

    async def get_issue(self, issue_key: str, pat: str) -> JiraRemoteIssue:
        response = await self._safe_request(
            "GET", f"/issue/{quote(issue_key, safe='-')}", pat, Operation.JIRA_UPDATE
        )
        data = self._json_object(response, Operation.JIRA_UPDATE, required=("id", "key", "fields"))
        if not all(isinstance(data[name], str) for name in ("id", "key")) or not isinstance(data["fields"], Mapping):
            raise self._contract_error(Operation.JIRA_UPDATE)
        return JiraRemoteIssue(data["key"], data["id"], dict(data["fields"]))

    async def find_by_request_hash(
        self, project_key: str, request_hash: str, pat: str
    ) -> tuple[PublishedIssue, ...]:
        jql = f'project = "{project_key}" AND issue.property[dztgbot.request_hash].value = "{request_hash}"'
        response = await self._safe_request(
            "GET", "/search", pat, Operation.JIRA_CREATE, params={"jql": jql, "maxResults": 2}
        )
        data = self._json_object(response, Operation.JIRA_CREATE, required=("issues",))
        issues = data["issues"]
        if not isinstance(issues, list):
            raise self._contract_error(Operation.JIRA_CREATE)
        results: list[PublishedIssue] = []
        for item in issues[:2]:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not isinstance(item.get("key"), str):
                raise self._contract_error(Operation.JIRA_CREATE)
            results.append(PublishedIssue(item["key"], item["id"], f"{self._base_url}/browse/{quote(item['key'], safe='-')}"))
        return tuple(results)

    async def get_create_metadata(self, project_key: str, pat: str) -> JiraMetadata:
        scope = hashlib.sha256(pat.encode("utf-8")).hexdigest()[:16]
        cache_key = (project_key, scope)
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._metadata_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]
        response = await self._safe_request(
            "GET",
            "/issue/createmeta",
            pat,
            Operation.JIRA_CREATE,
            params={"projectKeys": project_key, "expand": "projects.issuetypes.fields"},
        )
        data = self._json_object(response, Operation.JIRA_CREATE, required=("projects",))
        metadata = self._parse_metadata(project_key, data)
        async with self._cache_lock:
            self._metadata_cache[cache_key] = (time.monotonic() + self._metadata_ttl, metadata)
        return metadata

    async def _safe_request(
        self, method: str, path: str, pat: str, operation: Operation, **kwargs: object
    ) -> httpx.Response:
        for attempt in range(self._safe_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    f"{self._api_url}{path}",
                    headers=self._headers(pat),
                    timeout=self._timeouts.as_httpx(),
                    **kwargs,
                )
            except httpx.TimeoutException as error:
                if attempt < self._safe_retries:
                    continue
                raise self._nonmutation_error(operation, ErrorKind.TIMEOUT, SafeErrorCode.TIMED_OUT) from error
            except httpx.TransportError as error:
                if attempt < self._safe_retries:
                    continue
                raise self._nonmutation_error(operation, ErrorKind.CONNECTIVITY, SafeErrorCode.CONNECTIVITY_FAILED) from error
            if response.status_code < 400:
                return response
            gateway_error = self._status_error(response, operation, mutating=False)
            if gateway_error.classification.retryability is Retryability.AUTOMATIC and attempt < self._safe_retries:
                delay = gateway_error.retry_after or 0
                if delay > 0:
                    await asyncio.sleep(min(delay, 5.0))
                continue
            raise gateway_error
        raise AssertionError("unreachable")

    async def _mutation_request(
        self,
        method: str,
        path: str,
        pat: str,
        operation: Operation,
        *,
        json_payload: object | None = None,
        files: object | None = None,
        attachment: bool = False,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                f"{self._api_url}{path}",
                headers=self._headers(pat, attachment=attachment),
                timeout=self._timeouts.as_httpx(),
                json=json_payload,
                files=files,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            kind = ErrorKind.TIMEOUT if isinstance(error, httpx.TimeoutException) else ErrorKind.CONNECTIVITY
            raise JiraGatewayError(classify_unknown_mutation_outcome(operation=operation, kind=kind)) from error
        if response.status_code >= 400:
            raise self._status_error(response, operation, mutating=True)
        return response

    @classmethod
    def _status_error(
        cls, response: httpx.Response, operation: Operation, *, mutating: bool
    ) -> JiraGatewayError:
        status = response.status_code
        retry_after = cls._retry_after(response.headers.get("Retry-After"))
        fields = cls._bounded_error_fields(response)
        if status == 401:
            kind, code, retryability = ErrorKind.AUTHENTICATION, SafeErrorCode.AUTHENTICATION_FAILED, Retryability.NEVER
        elif status == 403:
            kind, code, retryability = ErrorKind.PERMISSION, SafeErrorCode.PERMISSION_DENIED, Retryability.NEVER
        elif status == 409:
            kind, code, retryability = ErrorKind.CONFLICT, SafeErrorCode.PROVIDER_REJECTED, Retryability.EXPLICIT
        elif status == 429:
            kind, code, retryability = ErrorKind.RATE_LIMIT, SafeErrorCode.RATE_LIMITED, Retryability.AUTOMATIC
        elif status >= 500:
            kind, code, retryability = ErrorKind.CONNECTIVITY, SafeErrorCode.CONNECTIVITY_FAILED, Retryability.AUTOMATIC
        else:
            kind, code, retryability = ErrorKind.PROVIDER_REJECTION, SafeErrorCode.PROVIDER_REJECTED, Retryability.NEVER
        if mutating:
            if status >= 500:
                # A server failure can be emitted after the mutation committed;
                # conservatively require reconciliation instead of retrying.
                classification = classify_unknown_mutation_outcome(
                    operation=operation, kind=kind
                )
            else:
                classification = classify_definite_mutation_failure(
                    operation=operation,
                    kind=kind,
                    safe_code=code,
                    retryability=Retryability.EXPLICIT if retryability is not Retryability.NEVER else Retryability.NEVER,
                )
        else:
            classification = ErrorClassification(
                kind=kind,
                operation=operation,
                retryability=retryability,
                mutation_certainty=MutationCertainty.NOT_APPLICABLE,
                safe_code=code,
            )
        return JiraGatewayError(classification, status_code=status, retry_after=retry_after, field_codes=fields)

    @staticmethod
    def _nonmutation_error(operation: Operation, kind: ErrorKind, code: SafeErrorCode) -> JiraGatewayError:
        return JiraGatewayError(ErrorClassification(kind, operation, Retryability.EXPLICIT, MutationCertainty.NOT_APPLICABLE, code))

    @staticmethod
    def _contract_error(operation: Operation) -> JiraGatewayError:
        return JiraGatewayError(ErrorClassification(ErrorKind.PROVIDER_CONTRACT, operation, Retryability.NEVER, MutationCertainty.NOT_APPLICABLE, SafeErrorCode.PROVIDER_CONTRACT_FAILED))

    @classmethod
    def _json_object(
        cls,
        response: httpx.Response,
        operation: Operation,
        *,
        required: Sequence[str],
    ) -> Mapping[str, Any]:
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise cls._contract_error(operation) from error
        if not isinstance(data, Mapping) or any(key not in data for key in required):
            raise cls._contract_error(operation)
        return data

    @classmethod
    def _json_array(
        cls, response: httpx.Response, operation: Operation
    ) -> list[Any]:
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise cls._contract_error(operation) from error
        if not isinstance(data, list):
            raise cls._contract_error(operation)
        return data

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        try:
            parsed = float(value) if value is not None else None
        except ValueError:
            return None
        return parsed if parsed is not None and 0 <= parsed <= 300 else None

    @staticmethod
    def _bounded_error_fields(response: httpx.Response) -> tuple[str, ...]:
        raw = response.content[:MAX_ERROR_BODY_BYTES]
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            return ()
        if not isinstance(data, Mapping) or not isinstance(data.get("errors", {}), Mapping):
            return ()
        return tuple(sorted(str(key)[:64] for key in data["errors"].keys()))[:32]

    @classmethod
    def _parse_metadata(cls, project_key: str, data: Mapping[str, Any]) -> JiraMetadata:
        projects = data.get("projects")
        if not isinstance(projects, list):
            raise cls._contract_error(Operation.JIRA_CREATE)
        for project in projects:
            if isinstance(project, Mapping) and project.get("key") == project_key:
                issue_types = project.get("issuetypes")
                if not isinstance(issue_types, list):
                    break
                names = tuple(item["name"] for item in issue_types if isinstance(item, Mapping) and isinstance(item.get("name"), str))
                if names:
                    priorities: set[str] = set()
                    for issue_type in issue_types:
                        if not isinstance(issue_type, Mapping):
                            continue
                        fields = issue_type.get("fields")
                        priority = fields.get("priority") if isinstance(fields, Mapping) else None
                        allowed = priority.get("allowedValues") if isinstance(priority, Mapping) else None
                        if isinstance(allowed, list):
                            priorities.update(
                                value["name"]
                                for value in allowed
                                if isinstance(value, Mapping)
                                and isinstance(value.get("name"), str)
                            )
                    return JiraMetadata(
                        project_key,
                        names,
                        tuple(sorted(priorities)),
                        marker_supported=True,
                    )
        raise cls._contract_error(Operation.JIRA_CREATE)


__all__ = [
    "JiraGateway", "JiraGatewayError", "JiraIssueFields", "JiraMetadata",
    "JiraRemoteIssue", "JiraTimeouts", "canonical_issue_fields",
    "canonical_request_hash", "diff_issue_fields",
]
