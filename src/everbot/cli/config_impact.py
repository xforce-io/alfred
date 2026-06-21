"""配置变更爆炸半径(#94 件A)。

agent → llm key →(cloud, model_name)映射,并标出被多 agent 共用的 key —— 改某条 llm
的 model_name 会连带改掉所有共用它的 agent,本视图让"改这条影响谁"在改之前可查。
"""
from __future__ import annotations

from typing import Dict, List, Optional


def build_config_impact(agent_keys: Dict[str, str], llms: Dict[str, dict]) -> List[dict]:
    """汇总每个 agent 的 {agent, key, cloud, model_name, shared_with}。

    shared_with = 与本 agent 共用同一 llm key 的其它 agent(排序、去自身)。
    """
    rows: List[dict] = []
    for agent in sorted(agent_keys):
        key = agent_keys[agent]
        llm = llms.get(key) or {}
        shared_with = sorted(
            other for other, k in agent_keys.items() if k == key and other != agent
        )
        rows.append(
            {
                "agent": agent,
                "key": key,
                "cloud": llm.get("cloud"),
                "model_name": llm.get("model_name"),
                "shared_with": shared_with,
            }
        )
    return rows
