"""dolphin.yaml 风格 model 配置 → milkie agent.md ModelConfig 映射(sidecar 产品化奠基)。

milkie serve 用 `--agent <md>` 加载单 agent,其 frontmatter 的 `model:`(默认档)与
`models.<tier>:`(具名档,milkie#126)需要 ``{provider, model, adapter, baseUrl}``。
alfred 的 dolphin.yaml 用 ``llms.<name> = {cloud, model_name, type_api}`` +
``clouds.<cloud> = {api, api_key}`` 描述模型,本模块做这层翻译。

注意(已知 gap):milkie OpenAICompatibleAdapter 的 apiKey 仅从 env 读,serve 的
createGateway 不从 ModelConfig 传 key。两档若跨不同 cloud(不同 key)需 milkie 支持
per-model key 注入(后续 milkie issue);本映射只产出 endpoint/model,key 由 spawn 时
按 cloud 注入 env。
"""
from typing import Any, Dict

import yaml


def dolphin_model_to_milkie(llms: Dict[str, Any], clouds: Dict[str, Any], llm_name: str) -> Dict[str, str]:
    """``llms[llm_name]`` + 其 cloud → milkie ``ModelConfig`` dict。

    未知 llm_name → KeyError(fail fast,不静默产出半配置)。
    """
    llm = llms[llm_name]
    cloud = clouds[llm["cloud"]]
    return {
        "provider": llm["cloud"],
        "model": llm["model_name"],
        "adapter": "openai-compatible",  # dolphin type_api=openai → milkie openai-compatible
        "baseUrl": cloud["api"],
    }


def build_milkie_model_tiers(
    llms: Dict[str, Any], clouds: Dict[str, Any], *, default: str, fast: str
) -> Dict[str, Dict[str, str]]:
    """构造 milkie 两档 model:``default``(默认档)+ ``fast``(具名档,对应 dolphin fast 模型)。

    返回 ``{"default": ModelConfig, "fast": ModelConfig}``,供 agent.md 生成填
    ``model:`` 与 ``models.fast:``。
    """
    return {
        "default": dolphin_model_to_milkie(llms, clouds, default),
        "fast": dolphin_model_to_milkie(llms, clouds, fast),
    }


def build_milkie_agent_md(agent_id: str, system_prompt: str, tiers: Dict[str, Dict[str, str]]) -> str:
    """生成 milkie ``serve --agent`` 的 agent.md 文本。

    - frontmatter:``agentId`` + 单 ``react`` llm-state fsm + 默认档 ``model:``;
      若 tiers 含 ``fast``,加 ``models.fast:`` 具名档(milkie#126 tier);
    - body:``system_prompt``(milkie agent 的 systemPrompt)。

    fsm 用单 react llm-state —— alfred 的 agent 无显式 fsm,语义上即「单轮 LLM 响应 +
    工具」,与 milkie serve PoC 的 smoke agent 一致。
    """
    fm: Dict[str, Any] = {
        "agentId": agent_id,
        "version": "1.0.0",
        "fsm": {"states": [{"name": "react", "type": "llm", "instructions": "respond to the user"}]},
        "model": tiers["default"],
    }
    if "fast" in tiers:
        fm["models"] = {"fast": tiers["fast"]}
    front = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n{system_prompt}\n"
