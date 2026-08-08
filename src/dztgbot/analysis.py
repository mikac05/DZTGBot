"""Temporary compatibility facade for legacy analysis imports.

The canonical template and Gemini implementation live in ``domain.models`` and
``infrastructure.gemini_gateway``.  This module only translates the legacy
``ForwardedMessage``/``issuetype`` surface while Phase 6 moves callers to the
canonical contracts; it owns no workflow state and contains no provider logic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .core import ForwardedMessage
from .domain.models import (
    JiraTaskTemplate as CanonicalJiraTaskTemplate,
    MediaKind,
    SourceMessageRef,
)
from .infrastructure.gemini_gateway import GeminiGateway, GeminiGatewayError
from .rules import RulesStore


class JiraTaskTemplate(CanonicalJiraTaskTemplate):
    """Legacy spelling shim backed by the canonical domain entity.

    The inherited dataclass fields remain the sole stored representation.  The
    ``issuetype`` attribute is a read-only alias for canonical ``issue_type``.
    """

    def __init__(
        self,
        *,
        summary: str,
        description: str,
        issuetype: str | None = None,
        labels: Sequence[str] = (),
        priority: str,
        project_key: str | None,
        components: Sequence[str] = (),
        assignee: str | None = None,
        acceptance_criteria: Sequence[str] = (),
        issue_type: str | None = None,
    ) -> None:
        selected_type = issue_type if issue_type is not None else issuetype
        if not selected_type:
            raise ValueError("issue_type must not be empty")
        super().__init__(
            project_key=project_key or "",
            issue_type=selected_type,
            summary=summary,
            description=description,
            priority=priority,
            labels=tuple(labels),
            components=tuple(components),
            assignee=assignee or "",
            acceptance_criteria=list(acceptance_criteria),
        )

    @property
    def issuetype(self) -> str:
        """Return the legacy Jira field spelling without storing a duplicate."""

        return self.issue_type

    @classmethod
    def from_canonical(cls, template: CanonicalJiraTaskTemplate) -> JiraTaskTemplate:
        return cls(
            project_key=template.project_key,
            issue_type=template.issue_type,
            summary=template.summary,
            description=template.description,
            priority=template.priority,
            labels=template.labels,
            components=template.components,
            assignee=template.assignee,
            acceptance_criteria=template.acceptance_criteria,
        )


# The canonical classified exception is already safe to expose to old callers.
GeminiAnalysisError = GeminiGatewayError


class _RateLimitedCompatibilityError(RuntimeError):
    status_code = 429


class _LegacyGeminiClientAdapter:
    """Normalize the old response spelling before the canonical parser sees it."""

    def __init__(self, client: object) -> None:
        self._client = client
        self.aio = self
        self.models = self

    async def generate_content(self, **kwargs: object) -> object:
        models = getattr(getattr(self._client, "aio", self._client), "models", None)
        generate = getattr(models, "generate_content", None)
        if generate is None:
            raise AttributeError("generate_content")
        try:
            response = await generate(**kwargs)
        except Exception as error:
            # Older SDK/test doubles exposed 429 only in their exception text.
            if "429" in str(error):
                raise _RateLimitedCompatibilityError() from error
            raise
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, Mapping) and "issuetype" in parsed and "issue_type" not in parsed:
            normalized = dict(parsed)
            normalized["issue_type"] = normalized.pop("issuetype")
            try:
                setattr(response, "parsed", normalized)
            except (AttributeError, TypeError):
                class _Response:
                    pass

                replacement = _Response()
                replacement.parsed = normalized
                replacement.text = getattr(response, "text", None)
                return replacement
        return response


class GeminiAnalyzer:
    """Legacy call shape delegating all provider work to ``GeminiGateway``."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        rules_store: RulesStore,
        default_project_key: str | None = None,
    ) -> None:
        from google import genai

        self._timeout_seconds = timeout_seconds
        self._rules_store = rules_store
        self._default_project_key = default_project_key
        self._models = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
        self._current_model_index = 0
        self._client = genai.Client(api_key=api_key)
        self._gateway = self._build_gateway()

    def _build_gateway(self) -> GeminiGateway:
        gateway = GeminiGateway(
            client=_LegacyGeminiClientAdapter(self._client),
            models=tuple(self._models),
            deadline_seconds=self._timeout_seconds,
            max_retries=max(0, len(self._models) - 1),
            backoff_seconds=0,
        )
        gateway._preferred_model = self._current_model_index
        return gateway

    @property
    def current_model(self) -> str:
        return self._models[self._current_model_index]

    async def analyze(
        self, forwarded: ForwardedMessage | Sequence[ForwardedMessage]
    ) -> JiraTaskTemplate:
        messages = [forwarded] if isinstance(forwarded, ForwardedMessage) else list(forwarded)
        rules = await self._rules_store.current_rules()
        gateway = getattr(self, "_gateway", None)
        if gateway is None:
            gateway = self._build_gateway()
            self._gateway = gateway
        result = await gateway.analyze_messages(
            [_source_ref(message, index) for index, message in enumerate(messages, 1)],
            rules,
            self._default_project_key or "UNSPECIFIED",
        )
        self._current_model_index = gateway._preferred_model
        return JiraTaskTemplate.from_canonical(result)

    async def aclose(self) -> None:
        aio = getattr(self._client, "aio", None)
        close = getattr(aio, "aclose", None)
        if close is not None:
            await close()


def _source_ref(message: ForwardedMessage, index: int) -> SourceMessageRef:
    raw_media = getattr(message.media_type, "value", "text")
    media = {
        "photo": MediaKind.PHOTO,
        "document": MediaKind.DOCUMENT,
        "video": MediaKind.VIDEO,
        "voice": MediaKind.VOICE,
    }.get(raw_media, MediaKind.TEXT)
    return SourceMessageRef(
        message_id=index,
        chat_id=1,
        sender_id=1,
        text=message.text or "",
        media_kind=media,
    )


def jira_template_preview(template: CanonicalJiraTaskTemplate, image_count: int = 0) -> str:
    """Render the bounded legacy preview during the composition transition."""

    description = _bounded(template.description, 1200)
    labels = ", ".join(template.labels) if template.labels else "無"
    components = ", ".join(template.components) if template.components else "無"
    acceptance = _bounded(
        "\n".join(f"- {item}" for item in template.acceptance_criteria) or "無",
        1200,
    )
    img_text = f"{image_count} 張圖片" if image_count > 0 else "無"
    return (
        "📋 **Jira 工單草稿預覽**（尚未建立）\n\n"
        f"**標題 (Summary)**: {template.summary}\n"
        f"**類型 (Type)**: {template.issue_type}\n"
        f"**優先級 (Priority)**: {template.priority}\n"
        f"**專案 (Project)**: {template.project_key or '未指定'}\n"
        f"**經辦人 (Assignee)**: {template.assignee or '未指定'}\n"
        f"**標籤 (Labels)**: {labels}\n"
        f"**模組 (Components)**: {components}\n"
        f"**附圖 (Attachments)**: {img_text}\n\n"
        f"**詳細描述 (Description)**:\n{description}\n\n"
        f"**驗收標準 (Acceptance Criteria)**:\n{acceptance}"
    )[:4000]


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


def jira_template_editable_text(template: CanonicalJiraTaskTemplate) -> str:
    criteria = "\n".join(f"- {item}" for item in template.acceptance_criteria)
    return (
        f"標題: {template.summary or '(請輸入標題)'}\n"
        f"類型: {template.issue_type or 'Task'}\n"
        f"專案: {template.project_key or 'NGSSA3'}\n"
        f"優先級: {template.priority or 'Medium'}\n"
        f"描述:\n{template.description or '(請輸入描述)'}\n\n"
        f"驗收標準:\n{criteria or '- (請輸入驗收標準)'}"
    )


def parse_edited_template(
    raw_text: str, original: CanonicalJiraTaskTemplate
) -> JiraTaskTemplate:
    """Parse the legacy editable block into the canonical field representation."""

    values: dict[str, Any] = {
        "summary": original.summary,
        "issue_type": original.issue_type,
        "project_key": original.project_key,
        "priority": original.priority,
    }
    description: list[str] = []
    criteria: list[str] = []
    mode = "header"
    prefixes = {
        "標題": "summary",
        "标题": "summary",
        "summary": "summary",
        "類型": "issue_type",
        "类型": "issue_type",
        "type": "issue_type",
        "issuetype": "issue_type",
        "專案": "project_key",
        "项目": "project_key",
        "project": "project_key",
        "優先級": "priority",
        "优先级": "priority",
        "priority": "priority",
    }
    for line in raw_text.strip().splitlines():
        stripped = line.strip()
        normalized = stripped.replace("：", ":")
        key, separator, value = normalized.partition(":")
        lower_key = key.lower()
        if lower_key in {"描述", "description"}:
            mode = "description"
            if value.strip():
                description.append(value.strip())
            continue
        if lower_key in {"驗收標準", "验收标准", "acceptance criteria"}:
            mode = "criteria"
            if value.strip():
                criteria.append(value.strip().lstrip("-* "))
            continue
        if mode == "header" and separator and lower_key in prefixes:
            cleaned = value.split("(", 1)[0].strip()
            if cleaned and not cleaned.startswith("(請輸入"):
                values[prefixes[lower_key]] = cleaned
        elif mode == "description":
            description.append(line)
        elif mode == "criteria" and stripped:
            cleaned = stripped.lstrip("-* ").strip()
            if cleaned and not cleaned.startswith("(請輸入"):
                criteria.append(cleaned)
    return JiraTaskTemplate(
        summary=values["summary"],
        description="\n".join(description).strip() or original.description,
        issue_type=values["issue_type"],
        labels=original.labels,
        priority=values["priority"],
        project_key=values["project_key"],
        components=original.components,
        assignee=original.assignee,
        acceptance_criteria=criteria or original.acceptance_criteria,
    )


def validate_template_fields(template: CanonicalJiraTaskTemplate) -> list[str]:
    errors: list[str] = []
    if not template.summary.strip():
        errors.append("標題 (Summary) 不能為空。")
    elif len(template.summary) > 255:
        errors.append("標題 (Summary) 過長，最大長度為 255 個字元。")
    if not template.description.strip():
        errors.append("詳細描述 (Description) 不能為空。")
    if template.issue_type not in {"Task", "Epic", "缺陷", "優化", "优化"}:
        errors.append("工單類型不符合專案規範。")
    if template.project_key and not template.project_key.isalnum():
        errors.append("專案 Key 格式無效。")
    return errors


__all__ = [
    "GeminiAnalysisError",
    "GeminiAnalyzer",
    "JiraTaskTemplate",
    "jira_template_editable_text",
    "jira_template_preview",
    "parse_edited_template",
    "validate_template_fields",
]
