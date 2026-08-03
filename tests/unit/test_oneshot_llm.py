"""dolphin-free 一次性 LLM 单测(#38)—— httpx MockTransport,无真网络。"""
import httpx
import pytest

from src.everbot.core.agent.provider import oneshot_llm as os_mod
from src.everbot.core.agent.provider.model_config import ModelRoute, ResolvedModel
from src.everbot.core.agent.provider.oneshot_llm import OneshotLLMProvider


def _patch_route(monkeypatch):
    resolved = ResolvedModel(
        logical_name="m",
        route=ModelRoute(base_url="http://fake/v1", api_key="k", model="mm"),
        source="system_default",
    )
    # oneshot_llm binds resolve_model; patch the module reference.
    monkeypatch.setattr(os_mod, "resolve_model", lambda **k: resolved)


async def test_returns_content_on_success(monkeypatch):
    _patch_route(monkeypatch)
    cap = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cap["url"] = str(req.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": " hi there "}}]})

    p = OneshotLLMProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await p.call_llm(None, "say hi")
    assert out == "hi there"  # strip
    assert cap["url"] == "http://fake/v1/chat/completions"


async def test_raise_on_error_true_raises(monkeypatch):
    _patch_route(monkeypatch)
    p = OneshotLLMProvider(client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
    with pytest.raises(RuntimeError):
        await p.call_llm(None, "x", raise_on_error=True)


async def test_raise_on_error_false_returns_error_string(monkeypatch):
    _patch_route(monkeypatch)
    p = OneshotLLMProvider(client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
    out = await p.call_llm(None, "x", raise_on_error=False)
    assert "500" in out and "boom" in out  # 错误串作结果返回,不抛


async def test_bad_config_raises_when_raise_on_error(monkeypatch):
    def _boom(**k):
        raise ValueError("No model resolved")

    monkeypatch.setattr(os_mod, "resolve_model", _boom)
    p = OneshotLLMProvider()
    with pytest.raises(RuntimeError):
        await p.call_llm(None, "x", raise_on_error=True)

async def test_call_llm_forwards_agent_name_to_resolve_model(monkeypatch):
    """#193 P0d: per-call agent_name must reach resolve_model (not stuck at None)."""
    captured = {}

    def _resolve(**kwargs):
        captured.update(kwargs)
        return ResolvedModel(
            logical_name="agent-model",
            route=ModelRoute(base_url="http://fake/v1", api_key="k", model="mm"),
            source="agent",
        )

    monkeypatch.setattr(os_mod, "resolve_model", _resolve)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    p = OneshotLLMProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await p.call_llm(None, "hi", agent_name="demo_agent", fast=True)
    assert out == "ok"
    assert captured.get("agent_name") == "demo_agent"
    assert captured.get("tier") == "fast"


async def test_call_llm_uses_constructor_agent_name_when_kwarg_omitted(monkeypatch):
    captured = {}

    def _resolve(**kwargs):
        captured.update(kwargs)
        return ResolvedModel(
            logical_name="ctor",
            route=ModelRoute(base_url="http://fake/v1", api_key="k", model="mm"),
            source="agent",
        )

    monkeypatch.setattr(os_mod, "resolve_model", _resolve)
    p = OneshotLLMProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
            )
        ),
        agent_name="from-ctor",
    )
    await p.call_llm(None, "hi")
    assert captured.get("agent_name") == "from-ctor"

