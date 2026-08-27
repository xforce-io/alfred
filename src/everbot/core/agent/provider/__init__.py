import logging

from .base import AgentProvider

logger = logging.getLogger(__name__)

_provider_singleton: "AgentProvider | None" = None

# provider-name → singleton; one MilkieProvider fans out to all agents via its internal per-agent sidecar pool
_provider_by_name: dict = {}


def get_provider() -> AgentProvider:
    """Return the active AgentProvider。

    #38 起 dolphin 已彻底移除,milkie 是唯一 runtime。base_url 取自
    ``everbot.milkie.base_url``(仅 legacy 共享 serve 场景;per-agent 路径由 pool 自管)。
    """
    global _provider_singleton
    if _provider_singleton is None:
        from ....infra.config import get_config
        from .milkie.provider import MilkieProvider

        everbot_cfg = (get_config() or {}).get("everbot", {}) or {}
        milkie_cfg = everbot_cfg.get("milkie", {}) or {}
        base_url = milkie_cfg.get("base_url")  # None → per-agent pool
        _provider_singleton = MilkieProvider(base_url)
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


def _agent_runtime(agent_name: str, everbot_cfg: dict | None = None) -> str:
    """everbot.agents.<name>.runtime, default milkie."""
    cfg = everbot_cfg if everbot_cfg is not None else _load_everbot_cfg()
    agent_cfg = ((cfg.get("agents") or {}).get(agent_name) or {})
    runtime = agent_cfg.get("runtime") or "milkie"
    return str(runtime)


def agent_uses_grok_cli(agent_name: str | None, everbot_cfg: dict | None = None) -> bool:
    """True when this agent is configured to host-spawn grok CLI."""
    if not agent_name:
        return False
    return _agent_runtime(agent_name, everbot_cfg) == "grok-cli"


def _make_provider(name: str = "milkie") -> "AgentProvider":
    if name == "grok-cli":
        from .grok_cli.provider import GrokCliProvider
        return GrokCliProvider()
    from .milkie.provider import MilkieProvider
    return MilkieProvider()


def get_provider_for_agent(agent_name: str) -> "AgentProvider":
    """Per-agent provider 路由。

    ``everbot.agents.<name>.runtime: grok-cli`` → 宿主 spawn grok CLI;
    其余（缺省）→ 单例 MilkieProvider（内部 per-agent sidecar 池）。
    """
    runtime = _agent_runtime(agent_name)
    key = "grok-cli" if runtime == "grok-cli" else "milkie"
    cached = _provider_by_name.get(key)
    if cached is None:
        cached = _make_provider(key)
        _provider_by_name[key] = cached
    return cached


def oneshot_llm_provider() -> "AgentProvider":
    """Provider for stateless one-shot ``call_llm`` (memory extraction / history compression).

    ``call_llm`` is NOT agent-relative(单 prompt → 单回复)。改用 dolphin-free 的
    :class:`OneshotLLMProvider`(直连 OpenAI 兼容端点,模型路由读 config/models.yaml),
    去掉对 dolphin in-process LLMClient 的依赖(#38 去 dolphin)。
    """
    cached = _provider_by_name.get("oneshot")
    if cached is None:
        from .oneshot_llm import OneshotLLMProvider
        cached = OneshotLLMProvider()
        _provider_by_name["oneshot"] = cached
    return cached


def uses_dolphin_executor(agent) -> bool:
    """True only for legacy in-process dolphin agents that expose ``.executor``.

    Grok-cli / milkie handles have no executor; callers must skip
    ``agent.executor.context`` even if provider routing is wrong.
    """
    return getattr(agent, "executor", None) is not None


def provider_for(agent) -> "AgentProvider":
    """Return the provider that owns this agent OBJECT."""
    from .grok_cli.provider import GrokCliAgentHandle

    name = getattr(agent, "name", "") or ""
    is_grok = (
        isinstance(agent, GrokCliAgentHandle)
        or getattr(agent, "runtime", None) == "grok-cli"
        or (bool(name) and _agent_runtime(name) == "grok-cli")
    )
    key = "grok-cli" if is_grok else "milkie"
    cached = _provider_by_name.get(key)
    if cached is None:
        cached = _make_provider(key)
        _provider_by_name[key] = cached
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


__all__ = [
    "AgentProvider",
    "get_provider",
    "get_provider_for_agent",
    "agent_uses_grok_cli",
    "oneshot_llm_provider",
    "provider_for",
    "uses_dolphin_executor",
    "shutdown_all_providers",
    "reset_provider",
]
