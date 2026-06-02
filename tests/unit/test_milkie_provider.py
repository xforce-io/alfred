"""TDD C2: MilkieProvider 走 AgentProvider 契约。

run_turn(handle, message, ...) 产 dolphin ``_progress`` 流(统一中立契约),
turn_orchestrator 在其上套 policy。create_agent 返回 MilkieAgentHandle(base_url
+ context_id)。用 httpx MockTransport 注入预设 SSE 验证编排。
"""
import json

import httpx

from everbot.core.agent.provider.milkie.provider import MilkieAgentHandle, MilkieProvider


def _sse(*frames: tuple[str, dict]) -> str:
    return "".join(f"event: {ev}\ndata: {json.dumps(d)}\n\n" for ev, d in frames)


def _provider(sse_text: str, capture: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["payload"] = json.loads(request.content)
            capture["url"] = str(request.url)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_text.encode("utf-8")
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MilkieProvider("http://sidecar", client=client), client


async def test_create_agent_returns_handle():
    p, client = _provider("")
    try:
        h = await p.create_agent("smoke", "/ws")
        assert isinstance(h, MilkieAgentHandle)
        assert h.base_url == "http://sidecar"
        assert h.context_id  # 非空
    finally:
        await client.aclose()


async def test_run_turn_yields_llm_progress_deltas():
    sse = _sse(
        ("agent.run.started", {"contextId": "c"}),
        ("message_delta", {"text": "Hello, "}),
        ("message_delta", {"text": "world!"}),
        ("agent.run.completed", {"status": "completed", "output": "Hello, world!"}),
    )
    p, client = _provider(sse)
    try:
        events = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "hi")]
    finally:
        await client.aclose()
    assert events == [
        {"_progress": [{"stage": "llm", "delta": "Hello, ", "answer": "", "id": "llm"}]},
        {"_progress": [{"stage": "llm", "delta": "world!", "answer": "", "id": "llm"}]},
    ]


async def test_run_turn_sends_contextid_and_input_to_chat():
    cap: dict = {}
    p, client = _provider(_sse(("agent.run.completed", {"status": "completed", "output": ""})), cap)
    try:
        _ = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "ctx-9"), "say hi")]
    finally:
        await client.aclose()
    assert cap["payload"]["contextId"] == "ctx-9"
    assert cap["payload"]["input"] == "say hi"
    assert cap["url"].endswith("/chat")


async def test_run_turn_yields_skill_progress_for_tools():
    sse = _sse(
        ("tool.requested", {"toolName": "t", "input": {"x": 1}, "toolCallId": "tc"}),
        ("tool.responded", {"toolName": "t", "toolCallId": "tc", "status": "ok", "output": "done"}),
        ("agent.run.completed", {"status": "completed", "output": "done"}),
    )
    p, client = _provider(sse)
    try:
        events = [e async for e in p.run_turn(MilkieAgentHandle("http://sidecar", "c"), "go")]
    finally:
        await client.aclose()
    items = [e["_progress"][0] for e in events]
    assert [it["stage"] for it in items] == ["skill", "skill"]
    assert items[0]["status"] == "running"
    assert items[1]["status"] == "completed"
    assert items[0]["id"] == items[1]["id"] == "tc"


async def test_self_built_client_disables_env_proxy():
    client = MilkieProvider("http://x")._new_client()
    try:
        assert client.trust_env is False
    finally:
        await client.aclose()
