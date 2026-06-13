"""dolphin-free 模型路由配置单测(#38)。"""
from pathlib import Path

from src.everbot.core.agent.provider.model_config import find_model_config_path, load_model_config


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


# ── #71: extra_body 解析与合并(fast 档关 thinking 的承载机制) ────


_YAML_EXTRA = """
default: deepseek-chat
fast: doubao-nothink
clouds:
  volcengine:
    api: https://ark.example.com/v3
    api_key: sk-ark
    extra_body:
      cloud_flag: true
      thinking:
        type: enabled
llms:
  deepseek-chat:
    cloud: volcengine
    model_name: deepseek-chat
  doubao-nothink:
    cloud: volcengine
    model_name: doubao-seed-2-0-pro
    extra_body:
      thinking:
        type: disabled
"""


def _write_extra(tmp_path) -> Path:
    p = tmp_path / "dolphin_extra.yaml"
    p.write_text(_YAML_EXTRA, encoding="utf-8")
    return p


def test_route_extra_body_defaults_empty(tmp_path):
    mc = load_model_config(_write(tmp_path))
    r = mc.route_for("qwen-turbo")
    assert r.extra_body == {}


def test_route_parses_llm_extra_body(tmp_path):
    mc = load_model_config(_write_extra(tmp_path))
    r = mc.route_for("doubao-nothink")
    assert r.extra_body["thinking"] == {"type": "disabled"}


def test_route_merges_cloud_and_llm_extra_body_llm_wins(tmp_path):
    mc = load_model_config(_write_extra(tmp_path))
    r = mc.route_for("doubao-nothink")
    # cloud 级键保留,llm 级同名键覆盖(同 headers 惯例)
    assert r.extra_body["cloud_flag"] is True
    assert r.extra_body["thinking"] == {"type": "disabled"}
    # 未配 extra_body 的 llm 仅继承 cloud 级
    r2 = mc.route_for("deepseek-chat")
    assert r2.extra_body == {"cloud_flag": True, "thinking": {"type": "enabled"}}


# ── #74: models.yaml 正名,dolphin.yaml legacy 兜底(同位置新名优先) ────


def _mk_cfg(base: Path, name: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / name
    p.write_text("default: x\n", encoding="utf-8")
    return p


def test_find_prefers_models_over_dolphin_in_same_location(tmp_path):
    home = tmp_path / "home"
    models = _mk_cfg(home, "models.yaml")
    _mk_cfg(home, "dolphin.yaml")
    got = find_model_config_path(
        home=home, cwd_config=tmp_path / "n1", repo_config=tmp_path / "n2")
    assert got == models


def test_find_legacy_home_dolphin_beats_lower_priority_models(tmp_path):
    """用户 home 级旧名覆盖必须仍优先于 cwd/repo 的新名 —— 改名不得悄换生效配置。"""
    home = tmp_path / "home"
    cwdc = tmp_path / "cwdc"
    legacy = _mk_cfg(home, "dolphin.yaml")
    _mk_cfg(cwdc, "models.yaml")
    got = find_model_config_path(home=home, cwd_config=cwdc, repo_config=tmp_path / "n")
    assert got == legacy


def test_find_falls_back_cwd_then_repo(tmp_path):
    cwdc = tmp_path / "cwdc"
    repoc = tmp_path / "repoc"
    _mk_cfg(repoc, "models.yaml")
    cwd_models = _mk_cfg(cwdc, "models.yaml")
    got = find_model_config_path(home=tmp_path / "n", cwd_config=cwdc, repo_config=repoc)
    assert got == cwd_models


def test_find_returns_none_when_nothing_exists(tmp_path):
    got = find_model_config_path(
        home=tmp_path / "a", cwd_config=tmp_path / "b", repo_config=tmp_path / "c")
    assert got is None
