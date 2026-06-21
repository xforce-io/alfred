
import json

import pytest

from src.everbot.core.agent.provider.milkie.launcher import (
    SidecarLauncher,
    LaunchSpec,
    SKILL_MANIFEST_ENV,
    SKILL_MANIFEST_FILENAME,
)


# discover_skills 输出形状(name/title/description/abs_path)。
_SKILLS = [
    {"name": "twitter-watch", "title": "Twitter Watch",
     "description": "抓取 X 用户最新推文", "abs_path": "/abs/twitter-watch"},
    {"name": "ops", "title": "Ops", "description": "", "abs_path": "/abs/ops"},
]


def _launcher(tmp_path):
    return SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data",
        node_bin="node",
        llms={"main": {"cloud": "oa", "model_name": "gpt-x", "type_api": "openai"},
              "fast": {"cloud": "oa", "model_name": "gpt-fast", "type_api": "openai"}},
        clouds={"oa": {"api": "https://api.oa/v1", "api_key": "sk-real"}},
        default_model="main",
        fast_model="fast",
    )


def test_build_writes_agent_md_and_returns_cmd(tmp_path):
    spec = _launcher(tmp_path).build("alice", system_prompt="You are Alice.")
    assert isinstance(spec, LaunchSpec)
    assert spec.agent_md.exists()
    text = spec.agent_md.read_text(encoding="utf-8")
    assert "You are Alice." in text
    assert "gpt-x" in text and "gpt-fast" in text
    assert spec.data_dir.is_dir()
    assert spec.cmd[0] == "node"
    assert spec.cmd[1].endswith("index.js")
    assert "serve" in spec.cmd
    assert spec.cmd[spec.cmd.index("--agent") + 1] == str(spec.agent_md)
    assert spec.cmd[spec.cmd.index("--port") + 1] == "0"
    assert spec.cmd[spec.cmd.index("--state-store") + 1] == "sqlite"
    assert spec.cmd[spec.cmd.index("--data-dir") + 1] == str(spec.data_dir)


def test_build_injects_cloud_api_key_env(tmp_path):
    spec = _launcher(tmp_path).build("alice", system_prompt="x")
    assert spec.env.get("OPENAI_API_KEY") == "sk-real"


def test_build_unknown_model_fails_fast(tmp_path):
    launcher = SidecarLauncher(
        dist_path=tmp_path / "x.js", data_dir_root=tmp_path / "d", node_bin="node",
        llms={}, clouds={}, default_model="missing", fast_model="missing",
    )
    with pytest.raises(KeyError):
        launcher.build("alice", system_prompt="x")


def test_build_no_api_key_skips_env_injection(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    launcher = SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data",
        node_bin="node",
        llms={"main": {"cloud": "oa", "model_name": "gpt-x", "type_api": "openai"},
              "fast": {"cloud": "oa", "model_name": "gpt-fast", "type_api": "openai"}},
        clouds={"oa": {"api": "https://api.oa/v1"}},  # 无 api_key
        default_model="main",
        fast_model="fast",
    )
    spec = launcher.build("alice", system_prompt="x")
    assert "OPENAI_API_KEY" not in spec.env


def test_build_scrubs_volcengine_token_for_non_volcengine_cloud(tmp_path, monkeypatch):
    # 防 milkie GatewayFactory 的 VOLCENGINE_TOKEN 抢占目标 key(实测 401 坑)。
    monkeypatch.setenv("VOLCENGINE_TOKEN", "volc-secret")
    monkeypatch.setenv("VOLCENGINE_API_BASE", "https://volc/api")
    spec = _launcher(tmp_path).build("alice", system_prompt="x")  # cloud=oa,非 volcengine
    assert spec.env.get("OPENAI_API_KEY") == "sk-real"
    assert "VOLCENGINE_TOKEN" not in spec.env
    assert "VOLCENGINE_API_BASE" not in spec.env


def test_build_keeps_volcengine_token_for_volcengine_cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_TOKEN", "volc-secret")
    launcher = SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data", node_bin="node",
        llms={"main": {"cloud": "volcengine", "model_name": "doubao", "type_api": "openai"}},
        clouds={"volcengine": {"api": "https://volc/api", "api_key": "volc-secret"}},
        default_model="main", fast_model="main",
    )
    spec = launcher.build("alice", system_prompt="x")
    assert spec.env.get("VOLCENGINE_TOKEN") == "volc-secret"  # volcengine agent 保留


def test_build_single_tier_agent_md_when_fast_equals_default(tmp_path):
    launcher = SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data",
        node_bin="node",
        llms={"main": {"cloud": "oa", "model_name": "gpt-x", "type_api": "openai"}},
        clouds={"oa": {"api": "https://api.oa/v1", "api_key": "sk-real"}},
        default_model="main",
        fast_model="main",  # fast == default
    )
    spec = launcher.build("alice", system_prompt="x")
    assert spec.agent_md.exists()
    assert "gpt-x" in spec.agent_md.read_text(encoding="utf-8")


def test_build_expands_env_in_api_key(tmp_path, monkeypatch):
    # #38:cloud api_key 含 ${ENV} 必须展开进 OPENAI_API_KEY,否则 milkie 拿字面 ${...} → 401。
    monkeypatch.setenv("MY_KEY_XYZ", "sk-real-expanded")
    launcher = SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data", node_bin="node",
        llms={"main": {"cloud": "oa", "model_name": "gpt-x", "type_api": "openai"}},
        clouds={"oa": {"api": "https://api.oa/v1", "api_key": "${MY_KEY_XYZ}"}},
        default_model="main", fast_model="main",
    )
    spec = launcher.build("alice", system_prompt="x")
    assert spec.env["OPENAI_API_KEY"] == "sk-real-expanded"


def test_build_uses_per_agent_model_override(tmp_path):
    # #38:default_model 覆盖 → agent.md 用该 agent 的模型,而非全局默认(实测 bug)。
    launcher = SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data", node_bin="node",
        llms={"glob": {"cloud": "oa", "model_name": "global-model", "type_api": "openai"},
              "mine": {"cloud": "vc", "model_name": "my-model", "type_api": "openai"}},
        clouds={"oa": {"api": "https://oa/v1", "api_key": "k1"},
                "vc": {"api": "https://vc/v1", "api_key": "k2"}},
        default_model="glob", fast_model="glob",
    )
    spec = launcher.build("alice", system_prompt="x", default_model="mine")
    import yaml
    fm = yaml.safe_load(spec.agent_md.read_text(encoding="utf-8").split("---")[1])
    assert fm["model"]["model"] == "my-model"        # 默认(chat)档用 per-agent 模型
    assert spec.env["OPENAI_API_KEY"] == "k2"        # 用 per-agent 模型的 cloud key
    assert fm["models"]["fast"]["model"] == "global-model"  # fast 档仍全局(单独 concern)


# ── skill_list manifest producer(milkie #139）─────────────────────────

def test_build_writes_skill_manifest_and_sets_env(tmp_path):
    spec = _launcher(tmp_path).build("alice", system_prompt="x", skills=_SKILLS)
    manifest_path = spec.data_dir / SKILL_MANIFEST_FILENAME
    assert manifest_path.exists()
    assert spec.env[SKILL_MANIFEST_ENV] == str(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 定稿 schema:每条目 {name, description, dir}（dir = discover_skills 的 abs_path）。
    assert data == {"skills": [
        {"name": "twitter-watch", "description": "抓取 X 用户最新推文", "dir": "/abs/twitter-watch"},
        {"name": "ops", "description": "", "dir": "/abs/ops"},
    ]}


def test_build_manifest_includes_every_discovered_skill(tmp_path):
    # 防"漏列"回归:manifest 技能集合 == 传入的 discover_skills 集合，一个不少。
    spec = _launcher(tmp_path).build("alice", system_prompt="x", skills=_SKILLS)
    data = json.loads((spec.data_dir / SKILL_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert {s["name"] for s in data["skills"]} == {s["name"] for s in _SKILLS}


def test_build_skills_none_writes_no_manifest(tmp_path):
    # 注入式 loader / reflector：skills=None → 不产出 manifest、不设 env（milkie 侧 degrade）。
    spec = _launcher(tmp_path).build("alice", system_prompt="x", skills=None)
    assert not (spec.data_dir / SKILL_MANIFEST_FILENAME).exists()
    assert SKILL_MANIFEST_ENV not in spec.env


def test_build_default_skills_arg_is_none(tmp_path):
    # 默认不传 skills 即 None → 向后兼容旧调用，不产出 manifest。
    spec = _launcher(tmp_path).build("alice", system_prompt="x")
    assert not (spec.data_dir / SKILL_MANIFEST_FILENAME).exists()
    assert SKILL_MANIFEST_ENV not in spec.env


def test_build_empty_skills_writes_configured_empty_manifest(tmp_path):
    # 空技能集(configured but empty)：写 {skills:[]}、设 env —— 比"未配置"更准确。
    spec = _launcher(tmp_path).build("alice", system_prompt="x", skills=[])
    manifest_path = spec.data_dir / SKILL_MANIFEST_FILENAME
    assert manifest_path.exists()
    assert spec.env[SKILL_MANIFEST_ENV] == str(manifest_path)
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"skills": []}


def test_build_manifest_missing_description_defaults_empty(tmp_path):
    spec = _launcher(tmp_path).build(
        "alice", system_prompt="x",
        skills=[{"name": "n", "title": "N", "abs_path": "/abs/n"}],  # 无 description 键
    )
    data = json.loads((spec.data_dir / SKILL_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert data["skills"][0] == {"name": "n", "description": "", "dir": "/abs/n"}


def test_build_manifest_is_valid_utf8_json_with_cjk(tmp_path):
    # ensure_ascii=False：中文按原样写入，不转义。
    spec = _launcher(tmp_path).build("alice", system_prompt="x", skills=_SKILLS)
    raw = (spec.data_dir / SKILL_MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert "抓取 X 用户最新推文" in raw  # 未被 \uXXXX 转义
    json.loads(raw)  # 合法 JSON


# ── E2b：sidecar OS 沙箱(#108)──────────────────────────────────────────────
import os

from src.everbot.core.agent.provider.milkie import launcher as _lmod
from src.everbot.core.agent.provider.milkie.launcher import (
    build_sandbox_profile,
    SANDBOX_PROFILE_FILENAME,
)


def _rp(p):
    return os.path.realpath(str(p))


def _sandbox_launcher(tmp_path, alfred_root):
    return SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data",
        node_bin="node",
        llms={"main": {"cloud": "oa", "model_name": "gpt-x", "type_api": "openai"},
              "fast": {"cloud": "oa", "model_name": "gpt-fast", "type_api": "openai"}},
        clouds={"oa": {"api": "https://api.oa/v1", "api_key": "sk-real"}},
        default_model="main",
        fast_model="fast",
        sandbox_enabled=True,
        alfred_root=alfred_root,
    )


def test_build_sandbox_profile_denies_shared_allows_workspace(tmp_path):
    root = tmp_path / ".alfred"
    (root / "skills").mkdir(parents=True)
    ws = root / "agents" / "alice"
    ws.mkdir(parents=True)
    (root / "config.yaml").write_text("x", encoding="utf-8")

    prof = build_sandbox_profile(alfred_root=root, agent_workspace=ws)

    assert "(allow default)" in prof
    assert f'(deny file-write* (subpath "{_rp(root / "skills")}"))' in prof
    assert f'(deny file-write* (subpath "{_rp(root / "agents")}"))' in prof
    assert f'(deny file-write* (literal "{_rp(root / "config.yaml")}"))' in prof
    # 自身 workspace 在 /agents 树下 → allow 必须排在 deny agents 之后(last-match-wins)。
    lines = prof.splitlines()
    deny_agents_i = next(
        i for i, l in enumerate(lines)
        if l.strip().startswith("(deny") and _rp(root / "agents") in l and _rp(ws) not in l
    )
    allow_ws_i = next(
        i for i, l in enumerate(lines)
        if l.strip().startswith("(allow") and _rp(ws) in l
    )
    assert allow_ws_i > deny_agents_i


def test_build_sandbox_profile_uses_realpath_not_symlink(tmp_path):
    # /tmp 软链坑:profile 必须写解析后的真实路径,否则 seatbelt subpath 静默失效。
    root = tmp_path / ".alfred"
    (root / "skills").mkdir(parents=True)
    ws = root / "agents" / "alice"
    ws.mkdir(parents=True)
    prof = build_sandbox_profile(alfred_root=root, agent_workspace=ws)
    assert _rp(root / "skills") in prof


def test_build_wraps_cmd_with_sandbox_when_enabled_on_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(_lmod, "_is_darwin", lambda: True)
    spec = _sandbox_launcher(tmp_path, tmp_path / ".alfred").build("alice", system_prompt="x")

    assert spec.cmd[0] == "sandbox-exec"
    assert spec.cmd[1] == "-f"
    assert spec.cmd[2] == str(spec.data_dir / SANDBOX_PROFILE_FILENAME)
    assert (spec.data_dir / SANDBOX_PROFILE_FILENAME).exists()
    # 原始 node serve 命令原样跟在后面。
    assert spec.cmd[3] == "node"
    assert "serve" in spec.cmd


def test_build_no_sandbox_when_disabled_by_default(tmp_path):
    # 灰度默认关:不传 sandbox_enabled → cmd 不被包裹。
    spec = _launcher(tmp_path).build("alice", system_prompt="x")
    assert spec.cmd[0] == "node"
    assert "sandbox-exec" not in spec.cmd


def test_build_skips_sandbox_on_non_darwin_with_warning(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(_lmod, "_is_darwin", lambda: False)
    with caplog.at_level(logging.WARNING, logger=_lmod.__name__):
        spec = _sandbox_launcher(tmp_path, tmp_path / ".alfred").build("alice", system_prompt="x")
    assert spec.cmd[0] == "node"
    assert "sandbox-exec" not in spec.cmd
    assert any("sandbox" in r.message.lower() for r in caplog.records)
