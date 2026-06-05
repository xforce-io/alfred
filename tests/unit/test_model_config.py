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


def test_route_merges_cloud_and_llm_headers(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "default: m\nfast: m\n"
        "clouds:\n  c:\n    api: http://x/v1\n    api_key: k\n    headers:\n      User-Agent: KimiCLI/0.77\n"
        "llms:\n  m:\n    cloud: c\n    model_name: mm\n",
        encoding="utf-8",
    )
    r = load_model_config(p).route()
    assert r.headers.get("User-Agent") == "KimiCLI/0.77"


def test_route_unset_env_placeholder_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_KEY_XYZ", raising=False)
    p = tmp_path / "e.yaml"
    p.write_text(
        "default: m\nfast: m\n"
        'clouds:\n  c:\n    api: http://x/v1\n    api_key: "${MISSING_KEY_XYZ}"\n'
        "llms:\n  m:\n    cloud: c\n    model_name: mm\n",
        encoding="utf-8",
    )
    import pytest
    with pytest.raises(ValueError, match="环境变量未设置"):
        load_model_config(p).route()  # ${MISSING_KEY_XYZ} 未设 → fail-fast,不泄漏 literal


def test_route_for_prefers_llm_level_api(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(
        "default: m\nfast: m\n"
        "clouds:\n  c:\n    api: http://cloud/v1\n    api_key: k\n"
        "llms:\n  m:\n    cloud: c\n    model_name: mm\n    api: http://llm-override/v1\n",
        encoding="utf-8",
    )
    assert load_model_config(p).route().base_url == "http://llm-override/v1"  # llm 级覆盖 cloud


def test_legacy_default_model_key_still_works(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text("default_model: m\nfast_llm: m\nllms:\n  m:\n    cloud: c\n    model_name: mm\n"
                 "clouds:\n  c:\n    api: http://x\n    api_key: k\n", encoding="utf-8")
    mc = load_model_config(p)
    assert mc.default_model == "m" and mc.fast_model == "m"


def test_repo_fast_tier_does_not_route_to_aliyun(monkeypatch):
    """Regression guard (#45): the shipped config/dolphin.yaml `fast` tier must
    not point at aliyun/dashscope. Background skill jobs (skill-evaluate Judge,
    reflection) resolve their model via cron.py:_resolve_skill_model() ->
    fast_model. An expired aliyun key there made every skill evaluation fail
    with 403 access_denied while normal chat (volcengine) kept working.
    """
    repo_config = Path(__file__).resolve().parents[2] / "config" / "dolphin.yaml"
    assert repo_config.exists(), repo_config
    # Provide volcengine creds so route_for() can expand ${...} placeholders.
    monkeypatch.setenv("VOLCENGINE_API_BASE", "https://volc.example/v1")
    monkeypatch.setenv("VOLCENGINE_TOKEN", "vk-test")
    mc = load_model_config(repo_config)
    route = mc.route(fast=True)
    assert "dashscope" not in route.base_url.lower(), route.base_url
    assert "aliyun" not in route.base_url.lower(), route.base_url
