from .base import AgentProvider

_provider_singleton: "AgentProvider | None" = None


def get_provider() -> AgentProvider:
    """Return the active AgentProvider, selected by ``everbot.provider``.

    Default ``dolphin`` (现有行为不变)。``milkie`` 时从 ``everbot.milkie.base_url``
    取 sidecar 地址构造 MilkieProvider(sidecar 生命周期自管理待后续)。
    """
    global _provider_singleton
    if _provider_singleton is None:
        from ....infra.config import get_config

        config = get_config() or {}
        everbot_cfg = config.get("everbot", {}) or {}
        name = everbot_cfg.get("provider") or "dolphin"
        if name == "milkie":
            from .milkie.provider import MilkieProvider

            milkie_cfg = everbot_cfg.get("milkie", {}) or {}
            base_url = milkie_cfg.get("base_url", "http://127.0.0.1:8723")
            _provider_singleton = MilkieProvider(base_url)
        else:
            from .dolphin.provider import DolphinProvider

            _provider_singleton = DolphinProvider()
    return _provider_singleton


def reset_provider() -> None:
    """Clear the cached provider singleton (tests / config reload)."""
    global _provider_singleton
    _provider_singleton = None


def __getattr__(name):
    # Lazy re-export of dolphin-backed Skillkit base classes so importing the
    # neutral package does not eagerly import dolphin.
    if name in ("SkillkitBase", "SkillFunction"):
        from .dolphin.skillkit import SkillkitBase, SkillFunction
        return {"SkillkitBase": SkillkitBase, "SkillFunction": SkillFunction}[name]
    raise AttributeError(name)


__all__ = [
    "AgentProvider",
    "get_provider",
    "reset_provider",
    "SkillkitBase",
    "SkillFunction",
]
