"""Correctness guard for ``provider_for(agent)`` type-dispatch routing.

The defect: agent CREATION routed per-agent (milkie/dolphin) but all OPERATIONS
used the GLOBAL provider, so a dolphin agent under a milkie global (or a milkie
agent under a dolphin global) had its operations run through the wrong provider.

``provider_for`` fixes this by dispatching on the agent OBJECT's type:
``MilkieAgentHandle`` → MilkieProvider, anything else → DolphinProvider —
independent of the global ``everbot.provider`` config.
"""
import pytest

from src.everbot.core.agent.provider import (
    oneshot_llm_provider,
    provider_for,
    reset_provider,
)
from src.everbot.core.agent.provider.milkie.provider import MilkieAgentHandle


@pytest.fixture(autouse=True)
def _reset():
    reset_provider()
    yield
    reset_provider()


def test_milkie_handle_routes_to_milkie_provider():
    handle = MilkieAgentHandle(name="a", base_url="u", context_id="c")
    provider = provider_for(handle)
    assert type(provider).__name__ == "MilkieProvider"


def test_provider_for_caches_singleton():
    # #38:dolphin 已移除,milkie 是唯一 provider → provider_for 恒返回同一单例。
    h1 = MilkieAgentHandle(name="a", base_url="u", context_id="c")
    h2 = MilkieAgentHandle(name="b", base_url="u2", context_id="c2")
    assert provider_for(h1) is provider_for(h2)


def test_oneshot_llm_provider_is_dolphin_free(monkeypatch):
    """#38:one-shot ``call_llm``(记忆抽取/历史压缩)改用 dolphin-free 的
    OneshotLLMProvider(直连 OpenAI 兼容),不再路由 dolphin。"""
    import src.everbot.infra.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_config", lambda: {"everbot": {"provider": "milkie"}}
    )
    reset_provider()

    assert type(oneshot_llm_provider()).__name__ == "OneshotLLMProvider"


def test_oneshot_llm_provider_caches_instance():
    """Repeated calls return the same cached one-shot provider instance."""
    assert oneshot_llm_provider() is oneshot_llm_provider()
