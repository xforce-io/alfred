from .base import AgentProvider

_provider_singleton: "AgentProvider | None" = None


def get_provider() -> AgentProvider:
    """Return the active AgentProvider (currently the only one: DolphinProvider)."""
    global _provider_singleton
    if _provider_singleton is None:
        from .dolphin.provider import DolphinProvider
        _provider_singleton = DolphinProvider()
    return _provider_singleton


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
    "SkillkitBase",
    "SkillFunction",
]
