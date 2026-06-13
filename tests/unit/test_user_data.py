"""Tests for UserDataManager."""

from pathlib import Path

from src.everbot.infra.user_data import UserDataManager


class TestAgentSkillDirs:
    def test_writable_skills_dir_is_under_agent_workspace(self, tmp_path: Path):
        udm = UserDataManager(alfred_home=tmp_path)
        result = udm.get_agent_writable_skills_dir("demo_agent")
        assert result == tmp_path / "agents" / "demo_agent" / "skills"

    def test_read_skill_dirs_priority_order(self, tmp_path: Path, monkeypatch):
        # Make repo_skills_dir resolve to a tmp dir so the test is hermetic.
        monkeypatch.setenv("ALFRED_REPO_ROOT", str(tmp_path / "fakerepo"))
        (tmp_path / "fakerepo" / "skills").mkdir(parents=True)

        udm = UserDataManager(alfred_home=tmp_path)
        dirs = udm.get_agent_read_skill_dirs("demo_agent")

        # priority 0: agent workspace ; priority 1: global ; priority 2: repo
        assert dirs[0] == tmp_path / "agents" / "demo_agent" / "skills"
        assert dirs[1] == tmp_path / "skills"
        assert dirs[2] == tmp_path / "fakerepo" / "skills"

    def test_read_skill_dirs_excludes_missing_repo(self, tmp_path: Path, monkeypatch):
        """If repo_skills_dir resolves to None, the chain has only 2 layers.

        Note: we patch the property directly because repo_skills_dir's real
        implementation walks up from __file__ looking for pyproject.toml,
        which during test runs always finds the alfred repo. We need to
        force the 'no repo' state explicitly.
        """
        udm = UserDataManager(alfred_home=tmp_path)
        monkeypatch.setattr(
            type(udm), "repo_skills_dir",
            property(lambda self: None),
        )
        dirs = udm.get_agent_read_skill_dirs("demo_agent")
        assert len(dirs) == 2
        assert dirs[0] == tmp_path / "agents" / "demo_agent" / "skills"
        assert dirs[1] == tmp_path / "skills"
