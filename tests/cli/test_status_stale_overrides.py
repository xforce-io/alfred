"""`bin/everbot status` surfaces stale per-agent skill overrides (#132, AC#3)."""

import os
from pathlib import Path

import importlib

from src.everbot.infra.user_data import UserDataManager

# NB: src.everbot.cli.__init__ re-exports a `main` function that shadows the
# same-named submodule on attribute access, so import the module explicitly.
main = importlib.import_module("src.everbot.cli.main")

OLD = 1_700_000_000.0
NEW = 1_700_100_000.0


def _write(path: Path, content: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.utime(path, (mtime, mtime))


def _udm_with_stale_override(tmp_path: Path, monkeypatch) -> UserDataManager:
    monkeypatch.setenv("ALFRED_REPO_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo" / "skills").mkdir(parents=True)
    udm = UserDataManager(alfred_home=tmp_path)
    _write(udm.repo_skills_dir / "twitter-watch" / "fetch.py", "fixed\n", NEW)
    _write(
        udm.get_agent_writable_skills_dir("demo") / "twitter-watch" / "fetch.py",
        "old\n", OLD,
    )
    return udm


def test_status_lists_stale_override(tmp_path, monkeypatch, capsys):
    udm = _udm_with_stale_override(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "get_user_data_manager", lambda: udm)
    monkeypatch.setattr(
        main, "get_config", lambda *a, **k: {"everbot": {"agents": {"demo": {}}}}
    )

    main._print_stale_skill_overrides()

    out = capsys.readouterr().out
    assert "demo" in out
    assert "twitter-watch" in out


def test_status_silent_when_no_stale_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_REPO_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo" / "skills").mkdir(parents=True)
    udm = UserDataManager(alfred_home=tmp_path)
    monkeypatch.setattr(main, "get_user_data_manager", lambda: udm)
    monkeypatch.setattr(
        main, "get_config", lambda *a, **k: {"everbot": {"agents": {"demo": {}}}}
    )

    main._print_stale_skill_overrides()

    assert capsys.readouterr().out == ""


def test_status_never_crashes_on_error(monkeypatch, capsys):
    # Degrade silently: status is a read-only self-check, must not raise.
    def boom():
        raise RuntimeError("config blew up")

    monkeypatch.setattr(main, "get_user_data_manager", boom)
    monkeypatch.setattr(main, "get_config", lambda *a, **k: {})

    main._print_stale_skill_overrides()  # must not raise

    assert capsys.readouterr().out == ""
