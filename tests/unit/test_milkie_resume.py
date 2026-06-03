# tests/unit/test_milkie_resume.py
import json

import httpx
import pytest

from everbot.core.agent.provider.milkie.provider import MilkieProvider, MilkieAgentHandle


async def test_resume_posts_to_resume_and_consumes_stream():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content.decode())
        body = (
            'event: message_delta\ndata: {"delta":"hi"}\n\n'
            'event: agent.run.completed\ndata: {"status":"completed","output":"hi"}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    prov = MilkieProvider.__new__(MilkieProvider)
    prov._base_url = "http://x"
    prov._client = client
    prov._sync_client = None
    prov._pool = None

    handle = MilkieAgentHandle(base_url="http://x", context_id="alice-1")
    await prov.resume(handle, "continue please")

    assert seen["url"].endswith("/resume")
    assert seen["json"]["contextId"] == "alice-1"
    assert seen["json"]["input"] == "continue please"
    await client.aclose()
