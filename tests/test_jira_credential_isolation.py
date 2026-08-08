from __future__ import annotations

import asyncio
import unittest
import httpx

from dztgbot.infrastructure.jira_gateway import JiraGateway


class JiraCredentialIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_tokens_remain_request_local(self) -> None:
        observed: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            observed.append(request.headers["Authorization"])
            return httpx.Response(200, json={"name": "user"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = JiraGateway(base_url="https://jira.invalid", client=client)
        self.assertEqual(client.headers.get("Authorization"), None)
        self.assertEqual(await asyncio.gather(gateway.test_credential("one"), gateway.test_credential("two")), [True, True])
        self.assertCountEqual(observed, ["Bearer one", "Bearer two"])
        self.assertEqual(client.headers.get("Authorization"), None)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
