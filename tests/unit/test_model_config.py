"""dolphin-free 模型路由配置单测(#38)。"""
from pathlib import Path

from src.everbot.core.agent.provider.model_config import load_model_config


_YAML = """
default: deepseek-chat
fast: qwen-turbo
clouds:
  deepseek:
    api: https://api.deepseek.com/v1
    api_key: "${TEST_DS_KEY}"
  aliyun:
    api: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: sk-aliyun
llms:
  deepseek-chat:
    cloud: deepseek
    model_name: deepseek-chat
  qwen-turbo:
    cloud: aliyun
    model_name: qwen-turbo-latest
"""


def _write(tmp_path) -> Path:
    p = tmp_path / "dolphin.yaml"
    p.write_text(_YAML, encoding="utf-8")
    return p


def test_loads_default_and_fast_keys(tmp_path):
    mc = load_model_config(_write(tmp_path))
    assert mc.default_model == "deepseek-chat"
    assert mc.fast_model == "qwen-turbo"


def test_route_resolves_base_model_and_expands_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DS_KEY", "sk-secret-ds")
    mc = load_model_config(_write(tmp_path))
    r = mc.route(fast=False)
    assert r.base_url == "https://api.deepseek.com/v1"
    assert r.model == "deepseek-chat"
    assert r.api_key == "sk-secret-ds"  # ${TEST_DS_KEY} 展开


def test_route_fast_picks_fast_tier(tmp_path):
    mc = load_model_config(_write(tmp_path))
    r = mc.route(fast=True)
    assert r.model == "qwen-turbo-latest"
    assert r.api_key == "sk-aliyun"


def test_missing_file_yields_empty(tmp_path):
    mc = load_model_config(tmp_path / "nope.yaml")
    assert mc.llms == {} and mc.default_model == ""


def test_legacy_default_model_key_still_works(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text("default_model: m\nfast_llm: m\nllms:\n  m:\n    cloud: c\n    model_name: mm\n"
                 "clouds:\n  c:\n    api: http://x\n    api_key: k\n", encoding="utf-8")
    mc = load_model_config(p)
    assert mc.default_model == "m" and mc.fast_model == "m"
