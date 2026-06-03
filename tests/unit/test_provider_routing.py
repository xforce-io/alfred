import pytest

from everbot.core.agent.provider import (
    get_provider_for_agent,
    reset_provider,
    shutdown_all_providers,
)


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


def test_global_default_dolphin_when_unset(monkeypatch):
    # 空配置:无 provider、无 explicit → 默认 dolphin
    _cfg(monkeypatch, {})
    assert type(get_provider_for_agent("alice")).__name__ == "DolphinProvider"


def test_global_dolphin_explicit_telegram_no_fallback(monkeypatch):
    # 全局 dolphin 时,telegram agent 不触发回退逻辑(本就 dolphin)
    _cfg(monkeypatch, {"provider": "dolphin",
                       "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
                       "agents": {}})
    assert type(get_provider_for_agent("alice")).__name__ == "DolphinProvider"


def test_telegram_config_absent_uses_global_milkie(monkeypatch):
    _cfg(monkeypatch, {"provider": "milkie", "agents": {}})
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"


def test_telegram_none_does_not_crash(monkeypatch):
    _cfg(monkeypatch, {"provider": "milkie", "channels": {"telegram": None}, "agents": {}})
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"


def test_telegram_dict_disabled_no_fallback(monkeypatch):
    # 单 bot enabled=False → 不算 telegram-serving → 用全局 milkie
    _cfg(monkeypatch, {"provider": "milkie",
                       "channels": {"telegram": {"enabled": False, "default_agent": "alice"}},
                       "agents": {}})
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"


def test_telegram_list_malformed_entries_ignored(monkeypatch):
    # 非 dict 条目 / 缺 default_agent 的条目被忽略,不崩
    _cfg(monkeypatch, {"provider": "milkie",
                       "channels": {"telegram": ["not-a-dict", {"enabled": True}]},
                       "agents": {}})
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"


def test_fallback_warns_once_per_agent(monkeypatch, caplog):
    import logging
    _cfg(monkeypatch, {"provider": "milkie",
                       "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
                       "agents": {}})
    with caplog.at_level(logging.WARNING):
        get_provider_for_agent("alice")
        get_provider_for_agent("alice")
    # 只 warn 一次
    warnings = [r for r in caplog.records if "自动回退 dolphin" in r.getMessage()]
    assert len(warnings) == 1


def test_singleton_identity_and_reset(monkeypatch):
    _cfg(monkeypatch, {"provider": "milkie", "agents": {}})
    a = get_provider_for_agent("x")
    b = get_provider_for_agent("y")
    assert a is b   # 同 provider-name 共享一个实例
    reset_provider()
    _cfg(monkeypatch, {"provider": "milkie", "agents": {}})  # reset 清了 _load 的 monkeypatch? 重设
    c = get_provider_for_agent("x")
    assert c is not a   # reset 后重建


async def test_shutdown_all_providers_covers_per_agent_cache(monkeypatch):
    import everbot.core.agent.provider as mod
    reset_provider()
    closed = []

    class _FakeMilkie:
        async def shutdown_sidecars(self):
            closed.append("milkie")

    # populate _provider_by_name via get_provider_for_agent with a milkie global
    _cfg(monkeypatch, {"provider": "milkie", "agents": {}})
    monkeypatch.setattr(mod, "_make_provider", lambda name: _FakeMilkie())
    p = get_provider_for_agent("alice")   # caches a _FakeMilkie under "milkie"
    await mod.shutdown_all_providers()
    assert closed == ["milkie"]   # the cached per-agent provider WAS shut down
    reset_provider()


async def test_shutdown_all_providers_dedups_same_instance(monkeypatch):
    import everbot.core.agent.provider as mod
    reset_provider()
    calls = []

    class _P:
        async def shutdown_sidecars(self):
            calls.append(1)

    inst = _P()
    mod._provider_singleton = inst
    mod._provider_by_name["milkie"] = inst   # same identity in both caches
    await mod.shutdown_all_providers()
    assert calls == [1]   # awaited once, not twice
    reset_provider()


async def test_shutdown_all_providers_skips_provider_without_method(monkeypatch):
    import everbot.core.agent.provider as mod
    reset_provider()

    class _NoMethod:
        pass

    closed = []

    class _Has:
        async def shutdown_sidecars(self):
            closed.append("has")

    mod._provider_by_name["dolphin"] = _NoMethod()   # no shutdown_sidecars
    mod._provider_by_name["milkie"] = _Has()
    await mod.shutdown_all_providers()   # must not raise
    assert closed == ["has"]
    reset_provider()
