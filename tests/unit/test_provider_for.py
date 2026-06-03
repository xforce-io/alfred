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


class _FakeDolphinAgent:
    """Any object that is NOT a MilkieAgentHandle → must route to dolphin."""


def test_milkie_handle_routes_to_milkie_provider():
    handle = MilkieAgentHandle(name="a", base_url="u", context_id="c")
    provider = provider_for(handle)
    assert type(provider).__name__ == "MilkieProvider"


def test_non_handle_routes_to_dolphin_provider():
    provider = provider_for(_FakeDolphinAgent())
    assert type(provider).__name__ == "DolphinProvider"


def test_provider_for_caches_per_type():
    h1 = MilkieAgentHandle(name="a", base_url="u", context_id="c")
    h2 = MilkieAgentHandle(name="b", base_url="u2", context_id="c2")
    assert provider_for(h1) is provider_for(h2)

    a1, a2 = _FakeDolphinAgent(), _FakeDolphinAgent()
    assert provider_for(a1) is provider_for(a2)


def test_dispatch_ignores_global_milkie_config(monkeypatch):
    """Global=milkie must NOT force a dolphin agent's operations onto milkie.

    This is the mixed-routing guard: even when ``everbot.provider == "milkie"``,
    operating on a dolphin agent OBJECT must go through DolphinProvider, and a
    milkie handle must go through MilkieProvider.
    """
    import src.everbot.infra.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_config", lambda: {"everbot": {"provider": "milkie"}}
    )
    reset_provider()

    dolphin_agent = _FakeDolphinAgent()
    assert type(provider_for(dolphin_agent)).__name__ == "DolphinProvider"

    handle = MilkieAgentHandle(name="a", base_url="u", context_id="c")
    assert type(provider_for(handle)).__name__ == "MilkieProvider"


def test_oneshot_llm_provider_routes_to_dolphin_under_milkie(monkeypatch):
    """One-shot ``call_llm`` (memory extraction / history compression) must go to
    dolphin even when the global ``everbot.provider`` is milkie.

    milkie's ``call_llm`` needs a fixed serve that the per-agent pool model does not
    provide, so these dolphin in-process features always route to dolphin.
    """
    import src.everbot.infra.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_config", lambda: {"everbot": {"provider": "milkie"}}
    )
    reset_provider()

    assert type(oneshot_llm_provider()).__name__ == "DolphinProvider"


def test_oneshot_llm_provider_caches_instance():
    """Repeated calls return the same cached dolphin provider instance."""
    assert oneshot_llm_provider() is oneshot_llm_provider()
