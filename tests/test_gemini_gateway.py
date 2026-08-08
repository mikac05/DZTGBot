from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from dztgbot.domain.errors import ErrorKind
from dztgbot.domain.models import MediaKind, SourceMessageRef
from dztgbot.infrastructure.gemini_gateway import GeminiGateway, GeminiGatewayError, PromptBudgets


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def message(text: str = "hello", kind: MediaKind = MediaKind.TEXT) -> SourceMessageRef:
    return SourceMessageRef(1, 1, 1, text, kind, NOW)


VALID = {
    "project_key": None,
    "issue_type": "Task",
    "summary": "summary",
    "description": "description",
    "priority": "Medium",
    "labels": ["telegram-intake"],
    "components": [],
    "assignee": None,
    "acceptance_criteria": ["works"],
}


class Models:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_content(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(parsed=VALID)


class GeminiGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_response_maps_to_domain_and_media_is_described(self) -> None:
        models = Models()
        gateway = GeminiGateway(client=SimpleNamespace(aio=SimpleNamespace(models=models)))
        result = await gateway.analyze_messages([message(kind=MediaKind.PHOTO)], "rules", "BOT")
        self.assertEqual(result.project_key, "BOT")
        self.assertEqual(result.issue_type, "Task")
        self.assertEqual(models.calls, 1)
        self.assertIn("image bytes are not supplied", gateway.build_prompt([message(kind=MediaKind.PHOTO)], "rules"))

    async def test_prompt_budgets_are_enforced_before_provider_call(self) -> None:
        models = Models()
        gateway = GeminiGateway(client=SimpleNamespace(aio=SimpleNamespace(models=models)), budgets=PromptBudgets(4, 5, 5))
        with self.assertRaisesRegex(ValueError, "message_character_budget"):
            await gateway.analyze_messages([message("12345")], "r", "BOT")
        self.assertEqual(models.calls, 0)

    async def test_typed_rate_limit_falls_back_but_not_sticky_outage(self) -> None:
        class RateLimited(Exception):
            status_code = 429

        class FallbackModels:
            def __init__(self): self.calls = []
            async def generate_content(self, model, **kwargs):
                self.calls.append(model)
                if len(self.calls) == 1: raise RateLimited()
                return SimpleNamespace(parsed=VALID)

        models = FallbackModels()
        gateway = GeminiGateway(client=SimpleNamespace(aio=SimpleNamespace(models=models)), models=("a", "b"), max_retries=1, backoff_seconds=0)
        result = await gateway.analyze_messages([message()], "r", "BOT")
        self.assertEqual(result.summary, "summary")
        self.assertEqual(models.calls, ["a", "b"])

    async def test_one_end_to_end_deadline(self) -> None:
        class Slow:
            async def generate_content(self, **kwargs):
                await asyncio.sleep(1)
        gateway = GeminiGateway(client=SimpleNamespace(aio=SimpleNamespace(models=Slow())), deadline_seconds=0.01)
        with self.assertRaises(GeminiGatewayError) as raised:
            await gateway.analyze_messages([message()], "r", "BOT")
        self.assertEqual(raised.exception.classification.kind, ErrorKind.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
