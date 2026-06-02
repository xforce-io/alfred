"""TDD: MilkieProvider.run_turn 编排。

POST /chat(SSE)→ SSEParser → adapter → 逐个 yield TurnEvent。用 httpx
MockTransport 注入预设 SSE,验证编排:delta 流 + 终态、请求体携带 contextId/
input、error 终态映射、非文本事件被忽略。
"""
import json

import httpx

from everbot.core.agent.provider.milkie.provider import MilkieProvider
from everbot.core.runtime.turn_policy import TurnEventType


def _sse(*frames: tuple[str, dict]) -> str:
    return "".join(f"event: {ev}\ndata: {json.dumps(d)}\n\n" for ev, d in frames)


def _provider(sse_text: str, capture: dict | None = None) -> tuple[MilkieProvider, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["payload"] = json.loads(request.content)
            capture["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_text.encode("utf-8"),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MilkieProvider("http://sidecar", client=client), client


async def test_run_turn_streams_deltas_then_terminal():
    sse = _sse(
        ("agent.run.started", {"contextId": "c1"}),
        ("message_delta", {"text": "Hello, "}),
        ("message_delta", {"text": "world!"}),
        ("agent.run.completed", {"status": "completed", "output": "Hello, world!"}),
    )
    provider, client = _provider(sse)
    try:
        events = [e async for e in provider.run_turn("hi", context_id="c1")]
    finally:
        await client.aclose()

    assert [(e.type, e.content) for e in events[:2]] == [
        (TurnEventType.LLM_DELTA, "Hello, "),
        (TurnEventType.LLM_DELTA, "world!"),
    ]
    assert events[-1].type == TurnEventType.TURN_COMPLETE
    assert events[-1].answer == "Hello, world!"
    assert events[-1].status == "completed"


async def test_run_turn_sends_contextid_and_input_to_chat():
    cap: dict = {}
    provider, client = _provider(
        _sse(("agent.run.completed", {"status": "completed", "output": ""})), cap
    )
    try:
        _ = [e async for e in provider.run_turn("say hi", context_id="ctx-9")]
    finally:
        await client.aclose()
    assert cap["payload"]["contextId"] == "ctx-9"
    assert cap["payload"]["input"] == "say hi"
    assert cap["url"].endswith("/chat")


async def test_run_turn_maps_error_terminal():
    sse = _sse(
        ("error", {"message": "kaboom"}),
        ("agent.run.completed", {"status": "error", "output": "", "error": "kaboom"}),
    )
    provider, client = _provider(sse)
    try:
        events = [e async for e in provider.run_turn("x", context_id="c")]
    finally:
        await client.aclose()
    assert len(events) == 1  # error 帧被忽略,仅终态产出
    assert events[0].type == TurnEventType.TURN_ERROR
    assert events[0].error == "kaboom"


async def test_self_built_client_disables_env_proxy():
    """连本地 sidecar 走回环,绝不能经系统代理。

    若读取 http_proxy/HTTPS_PROXY,httpx 会把 127.0.0.1 也代理掉,导致 /chat
    返回 502 —— e2e 实测踩到过。自建 client 必须 trust_env=False。
    """
    client = MilkieProvider("http://sidecar")._new_client()
    try:
        assert client.trust_env is False
    finally:
        await client.aclose()


async def test_run_turn_ignores_non_text_events():
    sse = _sse(
        ("agent.run.started", {"contextId": "c"}),
        ("tool.requested", {"toolName": "t"}),
        ("message_delta", {"text": "ok"}),
        ("agent.run.completed", {"status": "completed", "output": "ok"}),
    )
    provider, client = _provider(sse)
    try:
        events = [e async for e in provider.run_turn("x", context_id="c")]
    finally:
        await client.aclose()
    assert [e.type for e in events] == [
        TurnEventType.LLM_DELTA,
        TurnEventType.TURN_COMPLETE,
    ]
