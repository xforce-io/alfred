import logging

from .base import AgentProvider

logger = logging.getLogger(__name__)

_provider_singleton: "AgentProvider | None" = None

_warned_fallback: set = set()
# provider-name → singleton; one MilkieProvider fans out to all agents via its internal per-agent sidecar pool
_provider_by_name: dict = {}


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


def _load_everbot_cfg() -> dict:
    from ....infra.config import get_config
    return (get_config() or {}).get("everbot", {}) or {}


def _telegram_serving_agents(everbot_cfg: dict) -> set:
    """收集 telegram 频道绑定的 default_agent(单 bot dict 或多 bot list)。"""
    tg = (everbot_cfg.get("channels", {}) or {}).get("telegram")
    agents: set = set()
    if isinstance(tg, dict):
        if tg.get("enabled") and tg.get("default_agent"):
            agents.add(tg["default_agent"])
    elif isinstance(tg, list):
        for c in tg:
            if isinstance(c, dict) and c.get("enabled", True) and c.get("default_agent"):
                agents.add(c["default_agent"])
    return agents


def _make_provider(name: str) -> "AgentProvider":
    if name == "milkie":
        from .milkie.provider import MilkieProvider
        # intentionally no base_url: per-agent base_url comes from the spawned
        # sidecar; differs from legacy get_provider which passes a shared base_url
        return MilkieProvider()
    from .dolphin.provider import DolphinProvider
    return DolphinProvider()


def get_provider_for_agent(agent_name: str) -> "AgentProvider":
    """Per-agent provider 路由:显式配置 > telegram 自动回退 > 全局。"""
    everbot_cfg = _load_everbot_cfg()
    agent_cfg = (everbot_cfg.get("agents", {}) or {}).get(agent_name, {}) or {}
    explicit = agent_cfg.get("provider")
    global_name = everbot_cfg.get("provider") or "dolphin"

    if explicit:
        chosen = explicit
    elif global_name == "milkie" and agent_name in _telegram_serving_agents(everbot_cfg):
        chosen = "dolphin"
        if agent_name not in _warned_fallback:
            _warned_fallback.add(agent_name)
            logger.warning(
                "Agent '%s' 经 telegram 服务但 milkie 暂不支持 telegram skillkit"
                "(待 milkie#87),自动回退 dolphin。如确需 milkie 请显式配置 "
                "everbot.agents.%s.provider=milkie。", agent_name, agent_name,
            )
    else:
        chosen = global_name

    cached = _provider_by_name.get(chosen)
    if cached is None:
        cached = _make_provider(chosen)
        _provider_by_name[chosen] = cached
    return cached


def reset_provider() -> None:
    """Clear the cached provider singleton (tests / config reload)."""
    global _provider_singleton
    _provider_singleton = None
    _provider_by_name.clear()
    _warned_fallback.clear()


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
    "get_provider_for_agent",
    "reset_provider",
    "SkillkitBase",
    "SkillFunction",
]
