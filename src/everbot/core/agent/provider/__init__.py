import logging

from .base import AgentProvider

logger = logging.getLogger(__name__)

_provider_singleton: "AgentProvider | None" = None

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
    """Per-agent provider 路由:显式配置 > 全局。

    #38(telegram 原生化)后移除了"telegram-serving agent 在 milkie 下自动回退 dolphin"
    的分支 —— telegram 文件发送已改由 alfred channel 的输出约定(``<<<send_file: ...>>>``)
    提供,milkie 下 telegram agent 文本+文件均可用,不再需要回退兜底。
    """
    everbot_cfg = _load_everbot_cfg()
    agent_cfg = (everbot_cfg.get("agents", {}) or {}).get(agent_name, {}) or {}
    explicit = agent_cfg.get("provider")
    global_name = everbot_cfg.get("provider") or "dolphin"

    chosen = explicit if explicit else global_name

    cached = _provider_by_name.get(chosen)
    if cached is None:
        cached = _make_provider(chosen)
        _provider_by_name[chosen] = cached
    return cached


def oneshot_llm_provider() -> "AgentProvider":
    """Provider for stateless one-shot ``call_llm`` (memory extraction / history compression).

    ``call_llm`` is NOT agent-relative. These are dolphin in-process features (gated by
    ``needs_history_restore``); milkie's ``call_llm`` needs a fixed serve that the per-agent
    pool model does not provide, so one-shot LLM is routed to dolphin regardless of the
    global ``everbot.provider``.
    """
    cached = _provider_by_name.get("dolphin")
    if cached is None:
        cached = _make_provider("dolphin")
        _provider_by_name["dolphin"] = cached
    return cached


def provider_for(agent) -> "AgentProvider":
    """Return the provider that owns this agent OBJECT (dispatch by type).

    Operations (run_turn/export_session/variables/trajectory/state) MUST use the
    provider matching the agent's actual backend, not the global default — otherwise
    per-agent routing (explicit / telegram-fallback) breaks at the operation layer.
    A ``MilkieAgentHandle`` → milkie; any other agent object → dolphin.
    """
    from .milkie.provider import MilkieAgentHandle
    name = "milkie" if isinstance(agent, MilkieAgentHandle) else "dolphin"
    cached = _provider_by_name.get(name)
    if cached is None:
        cached = _make_provider(name)
        _provider_by_name[name] = cached
    return cached


async def shutdown_all_providers() -> None:
    """Close sidecars on every constructed provider (global singleton + per-agent-name cache).

    Agents are created via get_provider_for_agent → providers cached in _provider_by_name,
    where the milkie sidecar pools actually live. The global _provider_singleton is a separate
    instance. Shut down all of them (deduped by identity) so no milkie serve child leaks.
    """
    seen = set()
    providers = []
    if _provider_singleton is not None:
        providers.append(_provider_singleton)
    providers.extend(_provider_by_name.values())
    for p in providers:
        if id(p) in seen:
            continue
        seen.add(id(p))
        shutdown = getattr(p, "shutdown_sidecars", None)
        if shutdown is not None:
            await shutdown()


def reset_provider() -> None:
    """Clear the cached provider singleton (tests / config reload)."""
    global _provider_singleton
    _provider_singleton = None
    _provider_by_name.clear()


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
    "oneshot_llm_provider",
    "provider_for",
    "shutdown_all_providers",
    "reset_provider",
    "SkillkitBase",
    "SkillFunction",
]
