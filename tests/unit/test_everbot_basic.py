"""
EverBot 基础功能测试
"""

import pytest
from pathlib import Path
import tempfile

from src.everbot.infra.user_data import UserDataManager
from src.everbot.infra.workspace import WorkspaceLoader
from src.everbot.infra.config import load_config, save_config, get_default_config


class TestUserDataManager:
    """UserDataManager 测试"""

    def test_init(self):
        """测试初始化"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = UserDataManager(alfred_home=Path(tmpdir))
            assert manager.alfred_home == Path(tmpdir)
            assert manager.agents_dir == Path(tmpdir) / "agents"

    def test_ensure_directories(self):
        """测试目录创建"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = UserDataManager(alfred_home=Path(tmpdir))
            manager.ensure_directories()

            assert manager.agents_dir.exists()
            assert manager.sessions_dir.exists()
            assert manager.logs_dir.exists()

    def test_init_agent_workspace(self):
        """测试 Agent 工作区初始化"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = UserDataManager(alfred_home=Path(tmpdir))
            manager.ensure_directories()
            manager.init_agent_workspace("test_agent")

            agent_dir = manager.get_agent_dir("test_agent")
            assert agent_dir.exists()
            assert (agent_dir / "AGENTS.md").exists()
            assert (agent_dir / "HEARTBEAT.md").exists()
            assert (agent_dir / "MEMORY.md").exists()
            assert (agent_dir / "USER.md").exists()
            assert (agent_dir / "agent.dph").exists()

    def test_list_agents(self):
        """测试 Agent 列表"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = UserDataManager(alfred_home=Path(tmpdir))
            manager.ensure_directories()

            # 创建几个 Agent
            manager.init_agent_workspace("agent1")
            manager.init_agent_workspace("agent2")

            agents = manager.list_agents()
            assert "agent1" in agents
            assert "agent2" in agents
            assert len(agents) == 2


class TestWorkspaceLoader:
    """WorkspaceLoader 测试"""

    def test_load_empty(self):
        """测试加载空工作区"""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = WorkspaceLoader(Path(tmpdir))
            instructions = loader.load()

            assert instructions.agents_md is None
            assert instructions.user_md is None
            assert instructions.memory_md is None
            assert instructions.heartbeat_md is None

    def test_load_files(self):
        """测试加载工作区文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            # 创建测试文件
            (workspace / "AGENTS.md").write_text("# Test Agent")
            (workspace / "USER.md").write_text("# Test User")

            loader = WorkspaceLoader(workspace)
            instructions = loader.load()

            assert instructions.agents_md == "# Test Agent"
            assert instructions.user_md == "# Test User"
            assert instructions.memory_md is None

    def test_build_system_prompt(self):
        """测试构建系统提示"""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            (workspace / "AGENTS.md").write_text("Agent behavior")
            (workspace / "USER.md").write_text("User profile")

            loader = WorkspaceLoader(workspace)
            prompt = loader.build_system_prompt()

            assert "Agent behavior" in prompt
            assert "User profile" in prompt
            assert "---" in prompt


class TestConfig:
    """配置管理测试"""

    def test_default_config(self):
        """测试默认配置"""
        config = get_default_config()
        assert "everbot" in config
        assert "enabled" in config["everbot"]
        assert config["everbot"]["enabled"] is True

    def test_save_and_load(self):
        """测试保存和加载配置"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"

            # 保存
            config = {"test": "value"}
            save_config(config, str(config_path))

            # 加载
            loaded = load_config(str(config_path))
            assert loaded["test"] == "value"

    def test_load_config_respects_alfred_home(self, monkeypatch):
        """load_config() should use ALFRED_HOME when no explicit path given."""
        from src.everbot.infra.config import reset_config_cache
        reset_config_cache()

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("ALFRED_HOME", tmpdir)
            config_path = Path(tmpdir) / "config.yaml"

            # Write a config to ALFRED_HOME
            config = {"marker": "from_alfred_home"}
            save_config(config, str(config_path))

            # load_config with no args should find it via ALFRED_HOME
            loaded = load_config()
            assert loaded.get("marker") == "from_alfred_home"

    def test_save_config_respects_alfred_home(self, monkeypatch):
        """save_config() should use ALFRED_HOME when no explicit path given."""
        from src.everbot.infra.config import reset_config_cache
        reset_config_cache()

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("ALFRED_HOME", tmpdir)

            config = {"marker": "saved_to_alfred_home"}
            save_config(config)

            # Verify it was written to ALFRED_HOME/config.yaml
            config_path = Path(tmpdir) / "config.yaml"
            assert config_path.exists()
            loaded = load_config(str(config_path))
            assert loaded.get("marker") == "saved_to_alfred_home"


class TestCmdInit:
    """cmd_init 集成测试"""

    def test_init_writes_config_to_alfred_home(self, monkeypatch):
        """cmd_init should write config.yaml and workspace path under ALFRED_HOME."""
        from src.everbot.infra.config import reset_config_cache
        from src.everbot.infra.user_data import reset_user_data_manager
        reset_config_cache()
        reset_user_data_manager()

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("ALFRED_HOME", tmpdir)

            # Re-import to get fresh singleton
            from src.everbot.cli.main import cmd_init

            class FakeArgs:
                agent = "test_bot"

            cmd_init(FakeArgs())

            # Config should be in ALFRED_HOME
            config_path = Path(tmpdir) / "config.yaml"
            assert config_path.exists(), f"config.yaml not found in {tmpdir}"

            loaded = load_config(str(config_path))
            agent_cfg = loaded["everbot"]["agents"]["test_bot"]

            # workspace should point to ALFRED_HOME/agents/test_bot, not ~/.alfred
            assert "/.alfred/" not in agent_cfg["workspace"], \
                f"workspace should not contain hardcoded ~/.alfred: {agent_cfg['workspace']}"
            assert tmpdir in agent_cfg["workspace"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
