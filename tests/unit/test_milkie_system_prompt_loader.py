"""Task 8a: 真实 system_prompt loader 测试。

loader 必须:解析 agent_name → workspace dir,经 WorkspaceLoader 构建 system
prompt 并返回;workspace 不存在时 RAISE(milkie agent 无 prompt 即 bug,fail loud)。
"""
from pathlib import Path

import pytest

from src.everbot.core.agent.provider.milkie.provider import _default_system_prompt_loader


def test_loader_builds_prompt_from_workspace(tmp_path, monkeypatch):
    # 构造一个最小 agent workspace,放 SOUL.md
    agent_dir = tmp_path / "alice"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text(
        "I am Alice, a helpful assistant.", encoding="utf-8"
    )

    # 让 loader 把 agent_name 解析到这个 workspace
    import src.everbot.core.agent.provider.milkie.provider as mod
    monkeypatch.setattr(
        mod, "_resolve_agent_workspace", lambda name: agent_dir, raising=True
    )

    prompt = _default_system_prompt_loader("alice")
    assert "Alice" in prompt
    # build_system_prompt 给 SOUL.md 加 "# 身份定义" 段头
    assert "身份定义" in prompt


def test_loader_merges_multiple_instruction_files(tmp_path, monkeypatch):
    agent_dir = tmp_path / "bob"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("Bob soul.", encoding="utf-8")
    (agent_dir / "AGENTS.md").write_text("Bob behavior.", encoding="utf-8")

    import src.everbot.core.agent.provider.milkie.provider as mod
    monkeypatch.setattr(
        mod, "_resolve_agent_workspace", lambda name: agent_dir, raising=True
    )

    prompt = _default_system_prompt_loader("bob")
    assert "Bob soul." in prompt
    assert "Bob behavior." in prompt


def test_loader_raises_on_missing_workspace(monkeypatch):
    import src.everbot.core.agent.provider.milkie.provider as mod
    monkeypatch.setattr(
        mod,
        "_resolve_agent_workspace",
        lambda name: Path("/nonexistent/agent/dir"),
        raising=True,
    )
    with pytest.raises((FileNotFoundError, ValueError)):
        _default_system_prompt_loader("missing")


def test_resolve_agent_workspace_uses_user_data_manager(monkeypatch):
    """_resolve_agent_workspace 经 user-data manager 的 get_agent_dir 解析。"""
    import src.everbot.core.agent.provider.milkie.provider as mod

    expected = Path("/some/alfred/agents/carol")

    class _FakeUDM:
        def get_agent_dir(self, name):
            assert name == "carol"
            return expected

    monkeypatch.setattr(mod, "get_user_data_manager", lambda: _FakeUDM())
    assert mod._resolve_agent_workspace("carol") == expected
