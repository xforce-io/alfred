"""Doctor report tests."""

from pathlib import Path
import tempfile

from src.everbot.cli.doctor import collect_doctor_report, dolphin_has_system_skillkit
from src.everbot.infra.user_data import UserDataManager


def test_doctor_reports_missing_config_and_agents_dir():
    """Doctor should warn when config/agents are missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        home = root / ".alfred"
        UserDataManager(alfred_home=home)
        # Intentionally do not create directories or config files.
        items = collect_doctor_report(project_root=root, alfred_home=home)
        levels = [i.level for i in items]
        assert "WARN" in levels


def test_dolphin_has_system_skillkit_supports_tool_enabled_tools():
    """Doctor should recognize the renamed tool.enabled_tools config key."""
    assert dolphin_has_system_skillkit(
        {"tool": {"enabled_tools": ["system_skillkit", "resource_skillkit"]}}
    )


def test_dolphin_has_system_skillkit_supports_legacy_skill_enabled_skills():
    """Doctor should keep accepting the legacy skill.enabled_skills key."""
    assert dolphin_has_system_skillkit(
        {"skill": {"enabled_skills": ["system_skillkit"]}}
    )
