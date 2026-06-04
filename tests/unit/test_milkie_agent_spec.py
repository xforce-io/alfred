"""sidecar 产品化奠基:把 dolphin.yaml 风格 model 配置(llms/clouds + default/fast)
映射成 milkie agent.md 的两档 ModelConfig。纯函数,为 spawn serve 生成 --agent 文件铺路。
"""
import pytest

import yaml

from src.everbot.core.agent.provider.milkie.agent_spec import (
    dolphin_model_to_milkie,
    build_milkie_model_tiers,
    build_milkie_agent_md,
)


_LLMS = {
    "kimi-code": {"cloud": "kimi", "model_name": "kimi-for-coding", "type_api": "openai"},
    "qwen-turbo": {"cloud": "aliyun", "model_name": "qwen-turbo-latest", "type_api": "openai"},
}
_CLOUDS = {
    "kimi": {"api": "https://kimi.example/v1", "api_key": "sk-kimi"},
    "aliyun": {"api": "https://dashscope.example/compatible-mode/v1", "api_key": "sk-ali"},
}


def test_dolphin_model_to_milkie_expands_env_in_baseurl(monkeypatch):
    # #38:baseUrl 含 ${ENV} 必须展开,否则 milkie 拿到字面 ${...} 坏 URL。
    monkeypatch.setenv("MY_BASE_XYZ", "https://real.example/v3")
    llms = {"m": {"cloud": "c", "model_name": "mm", "type_api": "openai"}}
    clouds = {"c": {"api": "${MY_BASE_XYZ}", "api_key": "k"}}
    assert dolphin_model_to_milkie(llms, clouds, "m")["baseUrl"] == "https://real.example/v3"


def test_dolphin_model_to_milkie_unset_env_baseurl_fails_fast(monkeypatch):
    monkeypatch.delenv("UNSET_BASE_XYZ", raising=False)
    llms = {"m": {"cloud": "c", "model_name": "mm", "type_api": "openai"}}
    clouds = {"c": {"api": "${UNSET_BASE_XYZ}", "api_key": "k"}}
    with pytest.raises(ValueError, match="环境变量未设置"):
        dolphin_model_to_milkie(llms, clouds, "m")


def test_dolphin_model_to_milkie_maps_llm_and_cloud():
    assert dolphin_model_to_milkie(_LLMS, _CLOUDS, "qwen-turbo") == {
        "provider": "aliyun",
        "model": "qwen-turbo-latest",
        "adapter": "openai-compatible",
        "baseUrl": "https://dashscope.example/compatible-mode/v1",
    }


def test_build_milkie_model_tiers_default_and_fast():
    """default→默认档(agent.md `model:`),fast→具名档(`models.fast`,milkie#126 tier)。"""
    tiers = build_milkie_model_tiers(_LLMS, _CLOUDS, default="kimi-code", fast="qwen-turbo")
    assert tiers["default"]["model"] == "kimi-for-coding"
    assert tiers["default"]["baseUrl"] == "https://kimi.example/v1"
    assert tiers["fast"]["model"] == "qwen-turbo-latest"
    assert tiers["fast"]["provider"] == "aliyun"


def test_unknown_llm_raises():
    with pytest.raises(KeyError):
        dolphin_model_to_milkie(_LLMS, _CLOUDS, "nonexistent")


def test_build_agent_md_frontmatter_and_body():
    """生成 milkie agent.md:可被 YAML 解析的 frontmatter(agentId/fsm/两档 model)+
    systemPrompt 作为 body。milkie loadAgentFile 用 gray-matter 解析同结构。"""
    tiers = build_milkie_model_tiers(_LLMS, _CLOUDS, default="kimi-code", fast="qwen-turbo")
    md = build_milkie_agent_md("daily_insight", "You are a helpful agent.", tiers)

    head, _, body = md.partition("---\n")[2].partition("\n---\n")
    fm = yaml.safe_load(head)
    assert fm["agentId"] == "daily_insight"
    assert fm["fsm"]["states"][0]["type"] == "llm"  # 单 react llm state
    assert fm["model"]["model"] == "kimi-for-coding"
    assert fm["model"]["adapter"] == "openai-compatible"
    assert fm["models"]["fast"]["model"] == "qwen-turbo-latest"
    assert "You are a helpful agent." in body


def test_build_agent_md_without_fast_omits_models_block():
    """只有默认档(无 fast)时不写 models 块 —— tier='fast' 由 serve 回落 default(milkie#126)。"""
    tiers = {"default": dolphin_model_to_milkie(_LLMS, _CLOUDS, "kimi-code")}
    md = build_milkie_agent_md("a", "sp", tiers)
    fm = yaml.safe_load(md.partition("---\n")[2].partition("\n---\n")[0])
    assert "models" not in fm
    assert fm["model"]["model"] == "kimi-for-coding"
