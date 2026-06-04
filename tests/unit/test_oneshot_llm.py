"""dolphin-free 一次性 LLM 单测(#38)—— httpx MockTransport,无真网络。"""
import httpx
import pytest

from src.everbot.core.agent.provider import oneshot_llm as os_mod
from src.everbot.core.agent.provider.model_config import ModelConfig
from src.everbot.core.agent.provider.oneshot_llm import OneshotLLMProvider


def _patch_route(monkeypatch):
    cfg = ModelConfig(
        llms={"m": {"cloud": "c", "model_name": "mm"}},
        clouds={"c": {"api": "http://fake/v1", "api_key": "k"}},
        default_model="m", fast_model="m",
    )
    # oneshot_llm 直接绑定了 load_model_config,故 patch 其模块内引用。
    monkeypatch.setattr(os_mod, "load_model_config", lambda *a, **k: cfg)


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
    monkeypatch.setattr(os_mod, "load_model_config",
                        lambda *a, **k: ModelConfig({}, {}, "", ""))  # 空配置 → route KeyError
    p = OneshotLLMProvider()
    with pytest.raises(RuntimeError):
        await p.call_llm(None, "x", raise_on_error=True)
