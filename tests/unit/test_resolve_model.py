"""#155: single-source model resolution (intent + route table)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.everbot.core.agent.provider.model_config import (
    load_model_config,
    resolve_logical_model_name,
    resolve_model,
)


_YAML = """
default: kimi-code
fast: doubao-nothink
clouds:
  kimi:
    api: https://api.kimi.example/v1
    api_key: sk-kimi
  volcengine:
    api: https://ark.example/v3
    api_key: sk-volc
  deepseek:
    api: https://api.deepseek.com/v1
    api_key: sk-ds
llms:
  kimi-code:
    cloud: kimi
    model_name: kimi-for-coding
  deepseek-volcengine:
    cloud: volcengine
    model_name: glm-5.2
  doubao-nothink:
    cloud: volcengine
    model_name: doubao-seed
  deepseek-chat:
    cloud: deepseek
    model_name: deepseek-chat
"""


def _mc(tmp_path: Path):
    p = tmp_path / "models.yaml"
    p.write_text(_YAML, encoding="utf-8")
    return load_model_config(p)


def test_override_beats_agent_and_system(tmp_path):
    mc = _mc(tmp_path)
    with patch(
        "src.everbot.core.agent.agent_config.resolve_agent_model",
        return_value="deepseek-volcengine",
    ):
        name, source = resolve_logical_model_name(
            agent_name="demo_agent",
            override="deepseek-chat",
            model_config=mc,
        )
    assert name == "deepseek-chat"
    assert source == "override"


def test_agent_default_beats_system_default(tmp_path):
    """With agent context, skill/oneshot default must not fall to models.yaml default (kimi)."""
    mc = _mc(tmp_path)
    with patch(
        "src.everbot.core.agent.agent_config.resolve_agent_model",
        return_value="deepseek-volcengine",
    ):
        resolved = resolve_model(agent_name="demo_agent", model_config=mc)
    assert resolved.logical_name == "deepseek-volcengine"
    assert resolved.source == "agent"
    assert resolved.route.model == "glm-5.2"
    assert "ark.example" in resolved.route.base_url


def test_no_agent_uses_system_default(tmp_path):
    mc = _mc(tmp_path)
    resolved = resolve_model(model_config=mc)
    assert resolved.logical_name == "kimi-code"
    assert resolved.source == "system_default"
    assert resolved.route.model == "kimi-for-coding"


def test_fast_tier_uses_system_fast_even_with_agent(tmp_path):
    """Background skill-eval keeps models.yaml fast; agent default is for default tier."""
    mc = _mc(tmp_path)
    with patch(
        "src.everbot.core.agent.agent_config.resolve_agent_model",
        return_value="deepseek-volcengine",
    ):
        resolved = resolve_model(agent_name="demo_agent", tier="fast", model_config=mc)
    assert resolved.logical_name == "doubao-nothink"
    assert resolved.source == "system_fast"


def test_fast_tier_falls_back_to_agent_when_system_fast_empty(tmp_path):
    yaml = _YAML.replace("fast: doubao-nothink", "fast: \"\"")
    p = tmp_path / "models.yaml"
    p.write_text(yaml, encoding="utf-8")
    mc = load_model_config(p)
    # empty fast may collapse to default in loader — force empty
    mc.fast_model = ""
    with patch(
        "src.everbot.core.agent.agent_config.resolve_agent_model",
        return_value="deepseek-volcengine",
    ):
        name, source = resolve_logical_model_name(
            agent_name="demo_agent", tier="fast", model_config=mc
        )
    assert name == "deepseek-volcengine"
    assert source == "agent"


def test_unknown_logical_name_fails_on_route(tmp_path):
    mc = _mc(tmp_path)
    with pytest.raises(KeyError, match="not in llms"):
        resolve_model(override="no-such-model", model_config=mc)


def test_empty_resolution_raises():
    from src.everbot.core.agent.provider.model_config import ModelConfig

    mc = ModelConfig(llms={}, clouds={}, default_model="", fast_model="")
    with pytest.raises(ValueError, match="No model resolved"):
        resolve_logical_model_name(model_config=mc)
