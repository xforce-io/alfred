"""DolphinProvider.call_llm 行为：消息构造、模型选择、错误前缀。"""
import pytest
from unittest.mock import patch, MagicMock


class _FakeConfig:
    def __init__(self, default_model=None, fast_llm=None):
        self.default_model = default_model
        self.fast_llm = fast_llm


class _FakeContext:
    def __init__(self, config):
        self._config = config

    def get_config(self):
        return self._config


def _mk_stream(chunks):
    async def _gen(*a, **k):
        for c in chunks:
            yield c
    return _gen


@pytest.mark.asyncio
async def test_call_llm_returns_stripped_content():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    fake_client = MagicMock()
    fake_client.mf_chat_stream = _mk_stream([{"content": "  hello  "}])
    with patch.object(mod, "LLMClient", return_value=fake_client):
        out = await mod.call_llm(_FakeContext(_FakeConfig(default_model="m1")), "hi")
    assert out == "hello"


@pytest.mark.asyncio
async def test_call_llm_fast_uses_fast_llm():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    captured = {}

    def _capture(*a, **k):
        captured["model"] = k.get("model")

        async def _gen():
            yield {"content": "x"}
        return _gen()

    fake_client = MagicMock()
    fake_client.mf_chat_stream = _capture
    with patch.object(mod, "LLMClient", return_value=fake_client):
        await mod.call_llm(_FakeContext(_FakeConfig(fast_llm="ft")), "p", fast=True)
    assert captured["model"] == "ft"


@pytest.mark.asyncio
async def test_call_llm_default_prefers_default_model():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    captured = {}

    def _capture(*a, **k):
        captured["model"] = k.get("model")

        async def _gen():
            yield {"content": "x"}
        return _gen()

    fake_client = MagicMock()
    fake_client.mf_chat_stream = _capture
    with patch.object(mod, "LLMClient", return_value=fake_client):
        await mod.call_llm(_FakeContext(_FakeConfig(default_model="dm", fast_llm="ft")), "p")
    assert captured["model"] == "dm"


@pytest.mark.asyncio
async def test_call_llm_raises_on_error_prefix():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    fake_client = MagicMock()
    fake_client.mf_chat_stream = _mk_stream([{"content": "❌ boom"}])
    with patch.object(mod, "LLMClient", return_value=fake_client):
        with pytest.raises(RuntimeError):
            await mod.call_llm(_FakeContext(_FakeConfig(default_model="m1")), "hi")
