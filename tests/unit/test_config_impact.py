"""TDD #94 件A:配置变更爆炸半径可见。

回应 #91 痛点:改 models.yaml 一条 llm key 的 model_name,会连带改掉所有共用该 key
的 agent(当天 demo_agent 改 glm-5.2 顺带改了 _reflector),改前无从得知影响谁。
"""
from src.everbot.cli.config_impact import build_config_impact

_LLMS = {
    "deepseek-volcengine": {"cloud": "volcengine", "model_name": "glm-5.2"},
    "kimi-code": {"cloud": "kimi", "model_name": "kimi-for-coding"},
}


def test_shared_key_lists_other_agents():
    rows = build_config_impact(
        {"demo_agent": "deepseek-volcengine", "_reflector": "deepseek-volcengine"},
        _LLMS,
    )
    by_agent = {r["agent"]: r for r in rows}
    assert by_agent["demo_agent"]["shared_with"] == ["_reflector"]
    assert by_agent["_reflector"]["shared_with"] == ["demo_agent"]
    # 解析出 cloud/model_name
    assert by_agent["demo_agent"]["cloud"] == "volcengine"
    assert by_agent["demo_agent"]["model_name"] == "glm-5.2"


def test_unique_key_has_no_shared():
    rows = build_config_impact(
        {"coding-master": "kimi-code", "demo_agent": "deepseek-volcengine"}, _LLMS
    )
    by_agent = {r["agent"]: r for r in rows}
    assert by_agent["coding-master"]["shared_with"] == []


def test_unknown_key_resolves_to_none():
    rows = build_config_impact({"x": "ghost-key"}, _LLMS)
    assert rows[0]["cloud"] is None
    assert rows[0]["model_name"] is None
    assert rows[0]["key"] == "ghost-key"
