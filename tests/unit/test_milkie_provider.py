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
    # create_agent 现经 pool spawn/复用 per-agent serve,handle 携带该 serve 的
    # base_url(动态端口),而非固定 config base_url。注入 fake pool 验证此契约。
    class _Sidecar:
        base_url = "http://sidecar"

    class _Pool:
        async def get_or_spawn(self, name):
            return _Sidecar()

    p = MilkieProvider("http://config-base", pool=_Pool())
    h = await p.create_agent("smoke", "/ws")
    assert isinstance(h, MilkieAgentHandle)
    assert h.base_url == "http://sidecar"
    assert h.context_id  # 非空


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


def test_safe_noop_methods_do_not_crash():
    """milkie 自带机制的接口:no-op,不崩(turn 层可用的前提)。"""
    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://x", "c")
    p.init_trajectory(h, "/t", overwrite=True)
    p.finalize_trajectory_on_error(h)
    p.set_session_id(h, "s")
    assert p.ensure_chat_compatibility() is False
    assert p.is_paused(h) is False
    assert p.is_error(h) is False
    assert p.is_user_interrupt_paused(h) is False
    assert p.has_skill(h, "x") is False


def test_set_variable_posts_to_context_set_endpoint():
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        p.set_variable(MilkieAgentHandle("http://sidecar", "c1"), "model_name", "claude")
    finally:
        client.close()
    assert cap["url"].endswith("/context/set")
    assert cap["body"] == {"contextId": "c1", "name": "model_name", "value": "claude"}


def test_get_variable_reads_from_context_get_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": "claude"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        val = p.get_variable(MilkieAgentHandle("http://sidecar", "c1"), "model_name")
    finally:
        client.close()
    assert val == "claude"


async def test_still_unsupported_methods_raise_clearly():
    """仍需 milkie 扩展的接口:明确 NotImplementedError(而非静默错误)。"""
    import pytest

    p = MilkieProvider("http://x")
    h = MilkieAgentHandle("http://x", "c")
    with pytest.raises(NotImplementedError):
        p.register_skillkit(h, object())


def _llm_provider(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MilkieProvider("http://x", client=client), client


async def test_call_llm_posts_canonical_request_and_returns_output():
    """prompt → POST /llm,canonical Message[] + 默认 tier/temperature;返回 strip 后 output。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": "  summary text  "})

    p, client = _llm_provider(handler)
    try:
        out = await p.call_llm(None, "compress this")
    finally:
        await client.aclose()
    assert cap["url"].endswith("/llm")
    assert cap["body"]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "compress this"}]}
    ]
    assert cap["body"]["tier"] == "default"
    assert cap["body"]["temperature"] == 0.3
    assert out == "summary text"


async def test_call_llm_fast_selects_fast_tier_and_temperature_passes():
    """fast=True → tier='fast'(命中 serve 的便宜快档);temperature 透传。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": "ok"})

    p, client = _llm_provider(handler)
    try:
        await p.call_llm(None, "p", temperature=0.1, fast=True)
    finally:
        await client.aclose()
    assert cap["body"]["tier"] == "fast"
    assert cap["body"]["temperature"] == 0.1


async def test_call_llm_raises_on_error_when_serve_errors():
    """raise_on_error=True(默认):serve 非200 → RuntimeError(含错误信息),不静默吞。"""
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "gateway boom"})

    p, client = _llm_provider(handler)
    try:
        with pytest.raises(RuntimeError, match="gateway boom"):
            await p.call_llm(None, "p")
    finally:
        await client.aclose()


async def test_call_llm_returns_error_text_when_raise_disabled():
    """raise_on_error=False(compressor 语义):serve 非200 → 返回错误串当结果,不抛。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "gateway boom"})

    p, client = _llm_provider(handler)
    try:
        out = await p.call_llm(None, "p", raise_on_error=False)
    finally:
        await client.aclose()
    assert "gateway boom" in out


def test_export_session_reads_history_and_translates_to_alfred_format():
    """export_session 走 /session/history(#128)→ canonical Message[] 翻成 alfred
    history 格式:assistant tool_use→tool_calls、tool→tool_call_id、content 数组→字符串。"""
    canonical = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result text"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": canonical})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        out = p.export_session(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        client.close()
    assert cap["url"].endswith("/session/history")
    assert cap["body"] == {"contextId": "c1"}
    assert out["history_messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "let me check", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "search", "arguments": '{"q": "x"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result text"},
        {"role": "assistant", "content": "done"},
    ]
    assert out["variables"] == {}


async def test_interrupt_posts_to_interrupt_endpoint():
    """MilkieProvider.interrupt 经 serve /interrupt 端点(contextId)跨进程发信号。"""
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"contextId": "c1", "signaled": True})

    p, client = _llm_provider(handler)
    try:
        await p.interrupt(MilkieAgentHandle("http://sidecar", "c1"))
    finally:
        await client.aclose()
    assert cap["url"].endswith("/interrupt")
    assert cap["body"] == {"contextId": "c1"}


async def test_resume_not_implemented_pending_design():
    """milkie /resume 是流式(产新 turn 事件),语义与 dolphin resume_with_input 不同,
    待单独设计;当前明确 NotImplementedError 而非静默错误。"""
    import pytest

    p = MilkieProvider("http://x")
    with pytest.raises(NotImplementedError):
        await p.resume(MilkieAgentHandle("http://x", "c"), "msg")


def test_milkie_does_not_need_history_restore():
    """milkie serve 用 sqlite/jsonl 自持久化(milkie#130),同 contextId 重启自动从
    checkpoint 恢复 → alfred 不需灌回历史。"""
    assert MilkieProvider("http://x").needs_history_restore() is False


def test_export_session_empty_on_no_session():
    """无该 context(serve 404)→ 返回空历史,不抛(新会话场景)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": 'No session for contextId "c"'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        p = MilkieProvider("http://x", sync_client=client)
        out = p.export_session(MilkieAgentHandle("http://sidecar", "c"))
    finally:
        client.close()
    assert out == {"history_messages": [], "variables": {}}


import pytest

from everbot.core.agent.provider.milkie.provider import MilkieProvider, MilkieAgentHandle


class _FakeSidecarStub:
    def __init__(self):
        self.closed = 0
    @property
    def base_url(self):
        return "http://127.0.0.1:19999"
    async def close(self):
        self.closed += 1


async def test_create_agent_uses_pool_base_url(monkeypatch):
    stub = _FakeSidecarStub()

    class _FakePool:
        async def get_or_spawn(self, name):
            return stub
        async def shutdown_all(self):
            await stub.close()

    prov = MilkieProvider.__new__(MilkieProvider)   # 跳过 __init__ 的 config 读取
    prov._base_url = None
    prov._client = None
    prov._sync_client = None
    prov._pool = _FakePool()
    prov._system_prompt_loader = lambda name: "sys"

    handle = await prov.create_agent("alice", workspace_path="/tmp")
    assert isinstance(handle, MilkieAgentHandle)
    assert handle.base_url == "http://127.0.0.1:19999"
    assert handle.context_id.startswith("alice-")

    await prov.shutdown_sidecars()
    assert stub.closed == 1
