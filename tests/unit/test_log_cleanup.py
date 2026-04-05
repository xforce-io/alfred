"""Tests for historical log cleanup helpers."""

from __future__ import annotations

import json
from pathlib import Path

from src.everbot.infra.log_cleanup import cleanup_alfred_logs
from src.everbot.infra.user_data import UserDataManager


def test_cleanup_redacts_historical_logs_and_creates_backup(tmp_path: Path):
    user_data = UserDataManager(alfred_home=tmp_path)
    user_data.ensure_directories()
    log_file = user_data.logs_dir / "everbot.out"
    log_file.write_text(
        'HTTP Request: GET https://api.telegram.org/bot123:SECRET/getUpdates "HTTP/1.1 200 OK"\n',
        encoding="utf-8",
    )

    summary = cleanup_alfred_logs(user_data=user_data, dry_run=False)

    assert summary.files_updated >= 1
    assert summary.lines_redacted == 1
    assert "***REDACTED***" in log_file.read_text(encoding="utf-8")
    backups = list(user_data.logs_dir.glob("everbot.out.bak_*"))
    assert backups


def test_cleanup_migrates_legacy_skill_log_schema(tmp_path: Path):
    user_data = UserDataManager(alfred_home=tmp_path)
    user_data.ensure_directories()
    user_data.init_agent_workspace("demo")
    logs_dir = user_data.get_agent_skill_logs_dir("demo")
    logs_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = logs_dir / "web-search.jsonl"
    legacy_file.write_text(
        json.dumps({
            "skill_id": "web-search",
            "skill_version": "baseline",
            "triggered_at": "2026-04-05T09:00:00+00:00",
            "context_before": "user: search",
            "skill_output": "我将执行这个任务",
            "context_after": "",
            "session_id": "sess1",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary = cleanup_alfred_logs(user_data=user_data, dry_run=False)

    assert summary.skill_segments_migrated == 1
    data = json.loads(legacy_file.read_text(encoding="utf-8").splitlines()[0])
    assert data["status"] == "completed"
    assert data["output_kind"] == "final"
    assert data["skill_output"] == ""
    backups = list(logs_dir.glob("web-search.jsonl.bak_*"))
    assert backups


def test_cleanup_dry_run_does_not_modify_files(tmp_path: Path):
    user_data = UserDataManager(alfred_home=tmp_path)
    user_data.ensure_directories()
    log_file = user_data.logs_dir / "heartbeat.log"
    original = "Authorization: Bearer secret-token\n"
    log_file.write_text(original, encoding="utf-8")

    summary = cleanup_alfred_logs(user_data=user_data, dry_run=True)

    assert summary.files_updated == 1
    assert log_file.read_text(encoding="utf-8") == original
    assert not list(user_data.logs_dir.glob("heartbeat.log.bak_*"))
