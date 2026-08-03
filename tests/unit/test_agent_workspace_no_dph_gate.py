"""#193 P0e — agent identity must not require agent.dph (milkie-only)."""
from __future__ import annotations

from pathlib import Path

from src.everbot.cli.doctor import collect_doctor_report
from src.everbot.infra.user_data import UserDataManager


def test_list_agents_includes_workspace_without_agent_dph(tmp_path: Path):
    home = tmp_path / "alfred"
    mgr = UserDataManager(alfred_home=home)
    mgr.ensure_directories()
    agent_dir = mgr.get_agent_dir("milkie_only")
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("# agent\n", encoding="utf-8")
    # Explicitly no agent.dph
    assert not (agent_dir / "agent.dph").exists()

    agents = mgr.list_agents()
    assert "milkie_only" in agents


def test_init_agent_workspace_does_not_write_agent_dph(tmp_path: Path):
    home = tmp_path / "alfred"
    mgr = UserDataManager(alfred_home=home)
    mgr.ensure_directories()
    mgr.init_agent_workspace("new_bot")

    agent_dir = mgr.get_agent_dir("new_bot")
    assert (agent_dir / "AGENTS.md").exists()
    assert (agent_dir / "SOUL.md").exists() or (agent_dir / "AGENTS.md").exists()
    assert not (agent_dir / "agent.dph").exists()


def test_doctor_does_not_warn_missing_agent_dph(tmp_path: Path):
    home = tmp_path / "alfred"
    mgr = UserDataManager(alfred_home=home)
    mgr.ensure_directories()
    agent_dir = mgr.get_agent_dir("demo")
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("# demo\n", encoding="utf-8")
    (agent_dir / "SOUL.md").write_text("# soul\n", encoding="utf-8")

    items = collect_doctor_report(project_root=tmp_path, alfred_home=home)
    dph_warns = [
        i
        for i in items
        if "agent.dph" in (i.details or "").lower()
        or "agent.dph" in (i.title or "").lower()
        or "dolphin" in (i.details or "").lower()
    ]
    assert dph_warns == [], f"unexpected dph/dolphin doctor items: {dph_warns}"
