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
        return f


def _build_global_skills(skill_names, with_meta=True):
    """
    构造模拟的 GlobalSkills 运行时对象。

    模拟真实的 Dolphin SDK 对象图：
      global_skills.installedSkillset
        .getSkill("_load_resource_skill")
          .owner_skillkit  →  ResourceSkillkit
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

    # _load_resource_skill function with owner_skillkit binding
    loader_skill = MagicMock()
    loader_skill.owner_skillkit = resource_skillkit

    # installedSkillset
    installed_skillset = MagicMock()
    installed_skillset.getSkill.return_value = loader_skill

    # GlobalSkills（注意：没有 skillkits 属性，这是旧代码的错误假设）
    global_skills = SimpleNamespace(
        installedSkillset=installed_skillset,
    )

    return global_skills


class TestExtractRuntimeAvailableSkills:
    """_extract_runtime_available_skills 核心路径测试"""

    def test_extracts_skills_via_owner_skillkit(self, factory):
        """核心回归测试：通过 installedSkillset → owner_skillkit 路径提取 skills"""
        gs = _build_global_skills(["coding-master", "dev-browser", "paper-discovery"])

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert len(result) == 3
        names = {s["name"] for s in result}
        assert names == {"coding-master", "dev-browser", "paper-discovery"}

    def test_returns_empty_when_no_installed_skillset(self, factory):
        """GlobalSkills 没有 installedSkillset 时返回空"""
        gs = SimpleNamespace()  # 无任何属性

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert result == []

    def test_returns_empty_when_loader_skill_not_found(self, factory):
        """installedSkillset 中找不到 _load_resource_skill 时返回空"""
        installed = MagicMock()
        installed.getSkill.return_value = None
        gs = SimpleNamespace(installedSkillset=installed)

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert result == []

    def test_returns_empty_when_no_owner_skillkit(self, factory):
        """_load_resource_skill 没有 owner_skillkit 时返回空"""
        loader_skill = SimpleNamespace()  # 无 owner_skillkit
        installed = MagicMock()
        installed.getSkill.return_value = loader_skill
        gs = SimpleNamespace(installedSkillset=installed)

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert result == []

    def test_skill_metadata_populated(self, factory):
        """验证 skill 的 title、description、path 正确填充"""
        gs = _build_global_skills(["coding-master"])

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert len(result) == 1
        skill = result[0]
        assert skill["name"] == "coding-master"
        assert skill["title"] == "coding-master"
        assert skill["description"] == "Description of coding-master"
        assert "coding-master" in skill["path"]

    def test_description_truncated_at_150(self, factory):
        """超长 description 被截断到 150 字符 + ..."""
        gs = _build_global_skills(["long-desc"])
        # 替换 meta 的 description 为超长字符串（需要覆盖 side_effect）
        skillkit = gs.installedSkillset.getSkill("_load_resource_skill").owner_skillkit
        long_desc = "A" * 200
        skillkit.get_skill_meta.side_effect = None
        skillkit.get_skill_meta.return_value = SimpleNamespace(
            name="long-desc",
            description=long_desc,
            base_path="",
        )

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert result[0]["description"] == "A" * 150 + "..."

    def test_meta_exception_does_not_break(self, factory):
        """get_skill_meta 抛异常时不影响整体提取"""
        gs = _build_global_skills(["good-skill", "bad-skill"])
        skillkit = gs.installedSkillset.getSkill("_load_resource_skill").owner_skillkit

        def flaky_meta(name):
            if name == "bad-skill":
                raise RuntimeError("broken")
            return SimpleNamespace(name=name, description=f"desc-{name}", base_path="")

        skillkit.get_skill_meta.side_effect = flaky_meta

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert len(result) == 2
        # bad-skill 仍然在列表中，只是没有 meta 信息
        bad = [s for s in result if s["name"] == "bad-skill"][0]
        assert bad["title"] == "bad-skill"  # fallback to name
        assert bad["description"] == ""

    def test_disabled_skills_filtered(self, factory):
        """disabled skills 被过滤掉"""
        gs = _build_global_skills(["coding-master", "dev-browser", "deprecated-skill"])

        with patch.object(factory, "_load_disabled_skills", return_value={"deprecated-skill"}):
            result = factory._extract_runtime_available_skills(gs)

        names = {s["name"] for s in result}
        assert "deprecated-skill" not in names
        assert names == {"coding-master", "dev-browser"}

    def test_no_meta_callable_still_works(self, factory):
        """get_skill_meta 不可调用时，skill 仍然被提取（只缺 meta）"""
        gs = _build_global_skills(["raw-skill"], with_meta=False)

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert len(result) == 1
        assert result[0]["name"] == "raw-skill"
        assert result[0]["title"] == "raw-skill"  # fallback
        assert result[0]["description"] == ""

    def test_home_path_replaced_with_tilde(self, factory):
        """base_path 中的 home 目录被替换为 ~"""
        gs = _build_global_skills(["my-skill"])
        skillkit = gs.installedSkillset.getSkill("_load_resource_skill").owner_skillkit
        home = str(Path.home())
        skillkit.get_skill_meta.side_effect = None
        skillkit.get_skill_meta.return_value = SimpleNamespace(
            name="my-skill",
            description="test",
            base_path=f"{home}/.alfred/skills/my-skill",
        )

        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

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
        gs = _build_global_skills(["coding-master", "dev-browser"])

        # 验证我们构造的 mock 跟真实对象一样没有 skillkits
        assert not hasattr(gs, "skillkits")

        # 但 skills 仍然能被正确提取
        with patch.object(factory, "_load_disabled_skills", return_value=set()):
            result = factory._extract_runtime_available_skills(gs)

        assert len(result) == 2
