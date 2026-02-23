"""
Doctor report tests.
"""

from pathlib import Path
import tempfile

from src.everbot.cli.doctor import collect_doctor_report
from src.everbot.infra.user_data import UserDataManager


def test_doctor_reports_missing_config_and_agents_dir():
    """Doctor should warn when config/agents are missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        home = root / ".alfred"
        user_data = UserDataManager(alfred_home=home)
        # Intentionally do not create directories or config files.
        items = collect_doctor_report(project_root=root, alfred_home=home)
        levels = [i.level for i in items]
        assert "WARN" in levels

