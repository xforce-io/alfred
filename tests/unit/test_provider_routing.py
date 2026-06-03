import pytest

from everbot.core.agent.provider import get_provider_for_agent, reset_provider


@pytest.fixture(autouse=True)
def _reset():
    reset_provider()
    yield
    reset_provider()


def _cfg(monkeypatch, everbot):
    import everbot.core.agent.provider as mod
    monkeypatch.setattr(mod, "_load_everbot_cfg", lambda: everbot)


def test_explicit_agent_provider_wins(monkeypatch):
    _cfg(monkeypatch, {"provider": "dolphin",
                       "agents": {"alice": {"provider": "milkie"}}})
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"


def test_global_milkie_telegram_agent_falls_back_to_dolphin(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
        "agents": {},
    })
    assert type(get_provider_for_agent("alice")).__name__ == "DolphinProvider"


def test_global_milkie_non_telegram_agent_uses_milkie(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
        "agents": {},
    })
    assert type(get_provider_for_agent("bob")).__name__ == "MilkieProvider"


def test_global_milkie_multibot_telegram_detection(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": [
            {"enabled": True, "default_agent": "alice"},
            {"enabled": True, "default_agent": "dev"},
        ]},
        "agents": {},
    })
    assert type(get_provider_for_agent("dev")).__name__ == "DolphinProvider"
    assert type(get_provider_for_agent("other")).__name__ == "MilkieProvider"


def test_explicit_milkie_telegram_agent_respected(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
        "agents": {"alice": {"provider": "milkie"}},
    })
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"
