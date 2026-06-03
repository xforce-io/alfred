"""TDD C3: get_provider() 按配置选 provider(dolphin 默认 / milkie 可切)。

`everbot.provider` 决定激活哪个 provider;缺省 dolphin(现有行为不变)。milkie
时从 `everbot.milkie.base_url` 取 sidecar 地址。reset_provider() 供测试清单例。
"""
import src.everbot.core.agent.provider as prov
import src.everbot.infra.config as cfg
from src.everbot.core.agent.provider.dolphin.provider import DolphinProvider
from src.everbot.core.agent.provider.milkie.provider import MilkieProvider


def _patch_config(monkeypatch, conf: dict):
    monkeypatch.setattr(cfg, "get_config", lambda *a, **k: conf)


def test_default_is_dolphin(monkeypatch):
    prov.reset_provider()
    _patch_config(monkeypatch, {})
    try:
        assert isinstance(prov.get_provider(), DolphinProvider)
    finally:
        prov.reset_provider()


def test_explicit_dolphin(monkeypatch):
    prov.reset_provider()
    _patch_config(monkeypatch, {"everbot": {"provider": "dolphin"}})
    try:
        assert isinstance(prov.get_provider(), DolphinProvider)
    finally:
        prov.reset_provider()


def test_milkie_selected_with_base_url(monkeypatch):
    prov.reset_provider()
    _patch_config(monkeypatch, {"everbot": {"provider": "milkie", "milkie": {"base_url": "http://127.0.0.1:9999"}}})
    try:
        p = prov.get_provider()
        assert isinstance(p, MilkieProvider)
    finally:
        prov.reset_provider()


def test_provider_is_singleton(monkeypatch):
    prov.reset_provider()
    _patch_config(monkeypatch, {})
    try:
        assert prov.get_provider() is prov.get_provider()
    finally:
        prov.reset_provider()
