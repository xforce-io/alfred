"""
测试 AgentFactory._extract_runtime_available_skills

验证 resource skills 能从 GlobalSkills 运行时对象中正确提取。
这是 skill 注入 agent prompt 的关键路径。

回归用例：修复前的代码使用不存在的 global_skills.skillkits 属性，
导致所有 resource skills 静默丢失，agent 永远看不到可用 skills。
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

from src.everbot.core.agent.factory import AgentFactory


@pytest.fixture
def factory():
    """创建 AgentFactory 实例（不依赖配置文件）"""
    with patch.object(AgentFactory, "__init__", lambda self, **kw: None):
        f = AgentFactory.__new__(AgentFactory)
        # Default: no per-agent skill filtering
        f._get_agent_skills_filter = lambda agent_name: (None, "all")
        return f


def _build_global_skills(skill_names, with_meta=True):
    """
    构造模拟的 GlobalSkills 运行时对象。

    模拟真实的 Dolphin SDK 对象图：
      global_skills.installedToolSet
        .getTool("_load_resource_skill")
          .owner_toolkit / owner_skillkit  →  ResourceSkillkit
            .get_available_skills() → [name, ...]
            .get_skill_meta(name) → SkillMeta
    """
    # SkillMeta per skill
    metas = {}
    for name in skill_names:
        meta = SimpleNamespace(
            name=name,
            description=f"Description of {name}",
            base_path=f"/home/user/.alfred/skills/{name}",
        )
        metas[name] = meta

    # ResourceSkillkit
    resource_skillkit = MagicMock()
    resource_skillkit.get_available_skills.return_value = list(skill_names)
    if with_meta:
        resource_skillkit.get_skill_meta.side_effect = lambda n: metas.get(n)
    else:
        resource_skillkit.get_skill_meta = None

    # _load_resource_skill function with owner binding
    loader_skill = MagicMock()
    loader_skill.owner_toolkit = resource_skillkit
    loader_skill.owner_skillkit = resource_skillkit

    # installedToolSet
    installed_skillset = MagicMock()
    installed_skillset.getTool.return_value = loader_skill

    # GlobalSkills（注意：没有 skillkits 属性，这是旧代码的错误假设）
    global_skills = SimpleNamespace(
        installedToolSet=installed_skillset,
    )

    return global_skills


class TestExtractRuntimeAvailableSkills:
    """_extract_runtime_available_skills 核心路径测试"""

    def test_extracts_skills_via_owner_skillkit(self, factory):
        """核心回归测试：通过 installedToolSet → owner_skillkit 路径提取 skills"""
        gs = _build_global_skills(["example-skill", "dev-browser", "paper-discovery"])

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert len(result) == 3
        names = {s["name"] for s in result}
        assert names == {"example-skill", "dev-browser", "paper-discovery"}

    def test_returns_empty_when_no_installed_skillset(self, factory):
        """GlobalSkills 没有 installedToolSet 时返回空"""
        gs = SimpleNamespace()  # 无任何属性

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert result == []

    def test_returns_empty_when_loader_skill_not_found(self, factory):
        """installedToolSet 中找不到 _load_resource_skill 时返回空"""
        installed = MagicMock()
        installed.getTool.return_value = None
        gs = SimpleNamespace(installedToolSet=installed)

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert result == []

    def test_returns_empty_when_no_owner_skillkit(self, factory):
        """_load_resource_skill 没有 owner binding 时返回空"""
        loader_skill = SimpleNamespace()  # 无 owner_skillkit
        installed = MagicMock()
        installed.getTool.return_value = loader_skill
        gs = SimpleNamespace(installedToolSet=installed)

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert result == []

    def test_extracts_skills_via_owner_toolkit(self, factory):
        """兼容 Dolphin tool 命名：owner_toolkit 也应能提取 skills"""
        gs = _build_global_skills(["kweaver-code-review"])
        loader_skill = gs.installedToolSet.getTool("_load_resource_skill")
        del loader_skill.owner_skillkit

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert [s["name"] for s in result] == ["kweaver-code-review"]

    def test_skill_metadata_populated(self, factory):
        """验证 skill 的 title、description、path 正确填充"""
        gs = _build_global_skills(["example-skill"])

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert len(result) == 1
        skill = result[0]
        assert skill["name"] == "example-skill"
        assert skill["title"] == "example-skill"
        assert skill["description"] == "Description of example-skill"
        assert "example-skill" in skill["path"]

    def test_description_truncated_at_150(self, factory):
        """超长 description 被截断到 150 字符 + ..."""
        gs = _build_global_skills(["long-desc"])
        # 替换 meta 的 description 为超长字符串（需要覆盖 side_effect）
        skillkit = gs.installedToolSet.getTool("_load_resource_skill").owner_skillkit
        long_desc = "A" * 200
        skillkit.get_skill_meta.side_effect = None
        skillkit.get_skill_meta.return_value = SimpleNamespace(
            name="long-desc",
            description=long_desc,
            base_path="",
        )

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert result[0]["description"] == "A" * 150 + "..."

    def test_meta_exception_does_not_break(self, factory):
        """get_skill_meta 抛异常时不影响整体提取"""
        gs = _build_global_skills(["good-skill", "bad-skill"])
        skillkit = gs.installedToolSet.getTool("_load_resource_skill").owner_skillkit

        def flaky_meta(name):
            if name == "bad-skill":
                raise RuntimeError("broken")
            return SimpleNamespace(name=name, description=f"desc-{name}", base_path="")

        skillkit.get_skill_meta.side_effect = flaky_meta

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert len(result) == 2
        # bad-skill 仍然在列表中，只是没有 meta 信息
        bad = [s for s in result if s["name"] == "bad-skill"][0]
        assert bad["title"] == "bad-skill"  # fallback to name
        assert bad["description"] == ""

    def test_disabled_skills_filtered(self, factory):
        """disabled skills 被过滤掉"""
        gs = _build_global_skills(["example-skill", "dev-browser", "deprecated-skill"])

        with patch.object(factory, "_load_disabled_skills", return_value={"deprecated-skill"}):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        names = {s["name"] for s in result}
        assert "deprecated-skill" not in names
        assert names == {"example-skill", "dev-browser"}

    def test_no_meta_callable_still_works(self, factory):
        """get_skill_meta 不可调用时，skill 仍然被提取（只缺 meta）"""
        gs = _build_global_skills(["raw-skill"], with_meta=False)

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert len(result) == 1
        assert result[0]["name"] == "raw-skill"
        assert result[0]["title"] == "raw-skill"  # fallback
        assert result[0]["description"] == ""

    def test_home_path_replaced_with_tilde(self, factory):
        """base_path 中的 home 目录被替换为 ~"""
        gs = _build_global_skills(["my-skill"])
        skillkit = gs.installedToolSet.getTool("_load_resource_skill").owner_skillkit
        home = str(Path.home())
        skillkit.get_skill_meta.side_effect = None
        skillkit.get_skill_meta.return_value = SimpleNamespace(
            name="my-skill",
            description="test",
            base_path=f"{home}/.alfred/skills/my-skill",
        )

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert result[0]["path"] == "~/.alfred/skills/my-skill"


class TestOldApiDoesNotWork:
    """
    验证旧的错误 API (global_skills.skillkits) 不可用。
    确保不会回退到错误路径。
    """

    def test_global_skills_has_no_skillkits_attribute(self, factory):
        """
        真实的 GlobalSkills 对象没有 skillkits 属性。
        旧代码 getattr(global_skills, 'skillkits', []) 总是返回 []。
        """
        gs = _build_global_skills(["example-skill", "dev-browser"])

        # 验证我们构造的 mock 跟真实对象一样没有 skillkits
        assert not hasattr(gs, "skillkits")

        # 但 skills 仍然能被正确提取
        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "test-agent")

        assert len(result) == 2


class TestPerAgentSkillsFilter:
    """Per-agent skills.include / skills.exclude filtering."""

    def test_include_filters_to_allowlist(self, factory):
        """skills.include only keeps listed skills"""
        gs = _build_global_skills(["example-skill", "dev-browser", "paper-discovery"])
        factory._get_agent_skills_filter = lambda name: ({"example-skill"}, "include")

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "my-agent")

        assert [s["name"] for s in result] == ["example-skill"]

    def test_exclude_removes_listed_skills(self, factory):
        """skills.exclude removes listed skills, keeps the rest"""
        gs = _build_global_skills(["example-skill", "dev-browser", "paper-discovery"])
        factory._get_agent_skills_filter = lambda name: ({"dev-browser"}, "exclude")

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "my-agent")

        names = {s["name"] for s in result}
        assert names == {"example-skill", "paper-discovery"}

    def test_unknown_skill_in_include_raises(self, factory):
        """Referencing a non-existent skill in include raises ValueError"""
        gs = _build_global_skills(["example-skill"])
        factory._get_agent_skills_filter = lambda name: ({"example-skill", "no-such-skill"}, "include")

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            with pytest.raises(ValueError, match="no-such-skill"):
                factory._extract_runtime_available_skills(gs, "my-agent")

    def test_unknown_skill_in_exclude_raises(self, factory):
        """Referencing a non-existent skill in exclude raises ValueError"""
        gs = _build_global_skills(["example-skill"])
        factory._get_agent_skills_filter = lambda name: ({"ghost-skill"}, "exclude")

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            with pytest.raises(ValueError, match="ghost-skill"):
                factory._extract_runtime_available_skills(gs, "my-agent")

    def test_no_filter_passes_all(self, factory):
        """When no filter configured, all skills pass through"""
        gs = _build_global_skills(["a", "b", "c"])
        factory._get_agent_skills_filter = lambda name: (None, "all")

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs, "my-agent")

        assert len(result) == 3
