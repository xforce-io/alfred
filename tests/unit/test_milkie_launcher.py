
import pytest

from everbot.core.agent.provider.milkie.launcher import SidecarLauncher, LaunchSpec


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
