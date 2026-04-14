"""Unit tests for macOS LaunchAgent helpers."""

from __future__ import annotations

from pathlib import Path

from src.everbot.cli.launch_agent import LAUNCH_AGENT_LABEL, build_launch_agent_plist


def test_build_launch_agent_plist_contains_expected_fields(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    alfred_home = tmp_path / ".alfred"
    (project_root / ".venv" / "bin").mkdir(parents=True)
    (project_root / ".venv" / "bin" / "activate").write_text("export VIRTUAL_ENV=1\n", encoding="utf-8")

    plist = build_launch_agent_plist(project_root=project_root, alfred_home=alfred_home)

    assert plist["Label"] == LAUNCH_AGENT_LABEL
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["WorkingDirectory"] == str(project_root.resolve())
    assert plist["ProgramArguments"][:2] == ["/bin/bash", "-lc"]
    shell_command = plist["ProgramArguments"][2]
    assert "exec python -m src.everbot.cli start" in shell_command
    assert f"export ALFRED_HOME='{alfred_home.resolve()}'" in shell_command
    assert plist["EnvironmentVariables"]["PYTHONPATH"] == str(project_root.resolve())
