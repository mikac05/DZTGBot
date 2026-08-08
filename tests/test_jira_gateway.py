from __future__ import annotations

import json
import unittest

import httpx

from dztgbot.domain.errors import MutationCertainty
from dztgbot.domain.models import JiraTaskTemplate
from dztgbot.infrastructure.jira_gateway import JiraGateway, JiraGatewayError


def template(summary: str = "summary") -> JiraTaskTemplate:
    return JiraTaskTemplate("BOT", "Bug", summary, "description", "High", ("one",), ("api",), "owner", ["works"])


class JiraGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_uses_reviewed_fields_and_marker_without_substitution(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(201, json={"id": "100", "key": "BOT-1"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = JiraGateway(base_url="https://jira.invalid", client=client)
        result = await gateway.create_issue(template("x" * 300), "pat-a", "hash-a")
        await client.aclose()

        body = json.loads(requests[0].content)
        self.assertEqual(body["fields"]["summary"], "x" * 300)
        self.assertEqual(body["fields"]["issuetype"], {"name": "Bug"})
        self.assertEqual(body["properties"][0]["value"], "hash-a")
        self.assertEqual(result.issue_key, "BOT-1")

    async def test_create_timeout_is_unknown_and_never_retried(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("test", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = JiraGateway(base_url="https://jira.invalid", client=client, safe_retries=5)
        with self.assertRaises(JiraGatewayError) as raised:
            await gateway.create_issue(template(), "pat-a")
        await client.aclose()
        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.classification.mutation_certainty, MutationCertainty.UNKNOWN)

    async def test_metadata_is_cached_but_auth_failure_is_not(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"projects": [{"key": "BOT", "issuetypes": [{"name": "Task"}]}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = JiraGateway(base_url="https://jira.invalid", client=client)
        first = await gateway.get_create_metadata("BOT", "pat")
        second = await gateway.get_create_metadata("BOT", "pat")
        await client.aclose()
        self.assertIs(first, second)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
