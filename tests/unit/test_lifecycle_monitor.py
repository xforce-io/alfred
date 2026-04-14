"""Unit tests for daemon lifecycle monitor."""

from __future__ import annotations

import json
from pathlib import Path

from src.everbot.cli.lifecycle_monitor import mark_unexpected_exit


def _write_snapshot(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_mark_unexpected_exit_marks_running_snapshot(tmp_path: Path) -> None:
    lifecycle_file = tmp_path / "everbot.lifecycle.json"
    _write_snapshot(
        lifecycle_file,
        {
            "run_id": "run_1",
            "status": "running",
            "graceful_shutdown": False,
            "last_alive_at": "2026-04-14T14:00:00",
        },
    )

    changed = mark_unexpected_exit(
        lifecycle_file,
        run_id="run_1",
        detected_at="2026-04-14T14:10:00",
    )

    assert changed is True
    snapshot = json.loads(lifecycle_file.read_text(encoding="utf-8"))
    assert snapshot["status"] == "terminated"
    assert snapshot["exit_reason"] == "monitor_detected_process_exit"
    assert snapshot["detected_dead_at"] == "2026-04-14T14:10:00"


def test_mark_unexpected_exit_ignores_graceful_shutdown(tmp_path: Path) -> None:
    lifecycle_file = tmp_path / "everbot.lifecycle.json"
    _write_snapshot(
        lifecycle_file,
        {
            "run_id": "run_1",
            "status": "stopped",
            "graceful_shutdown": True,
        },
    )

    changed = mark_unexpected_exit(lifecycle_file, run_id="run_1")

    assert changed is False
    snapshot = json.loads(lifecycle_file.read_text(encoding="utf-8"))
    assert snapshot["status"] == "stopped"
