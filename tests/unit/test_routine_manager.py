"""Unit tests for routine manager CRUD helpers."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.everbot.core.tasks.routine_manager import (
    RoutineManager,
    _detect_local_iana_timezone,
)
from src.everbot.core.tasks.task_manager import ParseStatus, parse_heartbeat_md


def _read_task_list(path: Path):
    parsed = parse_heartbeat_md(path.read_text(encoding="utf-8"))
    assert parsed.status == ParseStatus.OK
    assert parsed.task_list is not None
    return parsed.task_list


def test_add_routine_persists_v2_task_block(tmp_path: Path):
    manager = RoutineManager(tmp_path)
    created = manager.add_routine(
        title="Daily digest",
        description="summarize daily changes",
        schedule="1h",
        execution_mode="isolated",
        source="chat",
        now=datetime(2026, 2, 12, 12, 0, tzinfo=timezone.utc),
    )

    assert created["title"] == "Daily digest"
    assert created["execution_mode"] == "isolated"
    task_list = _read_task_list(tmp_path / "HEARTBEAT.md")
    assert task_list.version == 2
    assert len(task_list.tasks) == 1
    assert task_list.tasks[0].source == "chat"
    assert task_list.tasks[0].next_run_at is not None


def test_add_routine_rejects_duplicate_title_and_schedule(tmp_path: Path):
    manager = RoutineManager(tmp_path)
    manager.add_routine(title="Sync report", schedule="30m")
    with pytest.raises(ValueError, match="duplicate routine"):
        manager.add_routine(title="Sync report", schedule="30m")


def test_list_update_and_remove_routine(tmp_path: Path):
    manager = RoutineManager(tmp_path)
    created = manager.add_routine(
        title="Weekly cleanup",
        description="remove stale notes",
        schedule="1d",
        execution_mode="inline",
    )
    task_id = created["id"]

    listed = manager.list_routines()
    assert len(listed) == 1
    assert listed[0]["id"] == task_id

    updated = manager.update_routine(
        task_id,
        description="cleanup stale notes and logs",
        execution_mode="isolated",
        timezone_name="UTC",
    )
    assert updated is not None
    assert updated["execution_mode"] == "isolated"
    assert updated["description"] == "cleanup stale notes and logs"

    assert manager.remove_routine(task_id, soft_disable=True) is True
    listed_enabled = manager.list_routines(include_disabled=False)
    assert listed_enabled == []

    assert manager.remove_routine(task_id, soft_disable=False) is True
    listed_all = manager.list_routines(include_disabled=True)
    assert listed_all == []


def test_corrupted_heartbeat_raises_on_crud(tmp_path: Path):
    (tmp_path / "HEARTBEAT.md").write_text("# HEARTBEAT\n```json\n{invalid\n```\n", encoding="utf-8")
    manager = RoutineManager(tmp_path)
    with pytest.raises(ValueError, match="corrupted"):
        manager.list_routines()


def test_add_routine_auto_mode_infers_isolated_for_long_description(tmp_path: Path):
    manager = RoutineManager(tmp_path)
    created = manager.add_routine(
        title="Long analysis",
        description="x" * 260,
        execution_mode="auto",
        schedule="1d",
    )
    assert created["execution_mode"] == "isolated"


def test_add_routine_enforces_active_soft_limit(tmp_path: Path):
    manager = RoutineManager(tmp_path)
    for idx in range(manager.ACTIVE_ROUTINE_SOFT_LIMIT):
        manager.add_routine(
            title=f"Routine {idx}",
            schedule=f"{idx + 1}h",
            execution_mode="inline",
        )

    with pytest.raises(ValueError, match="soft limit"):
        manager.add_routine(
            title="Overflow routine",
            schedule="25h",
            execution_mode="inline",
        )


def test_add_routine_with_explicit_next_run_at(tmp_path: Path):
    """next_run_at parameter overrides computed schedule-based value."""
    manager = RoutineManager(tmp_path)
    explicit_time = "2026-06-01T09:00:00+00:00"

    # With schedule but explicit next_run_at — next_run_at wins
    created = manager.add_routine(
        title="Morning greeting",
        schedule="1d",
        next_run_at=explicit_time,
        now=datetime(2026, 2, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert created["next_run_at"] == explicit_time

    task_list = _read_task_list(tmp_path / "HEARTBEAT.md")
    assert task_list.tasks[0].next_run_at == explicit_time


def test_add_routine_one_shot_with_next_run_at_no_schedule(tmp_path: Path):
    """One-shot task: no schedule, only next_run_at."""
    manager = RoutineManager(tmp_path)
    explicit_time = "2026-02-13T12:02:00+08:00"

    created = manager.add_routine(
        title="Tell a joke",
        description="Tell a programmer joke",
        next_run_at=explicit_time,
    )
    assert created["next_run_at"] == explicit_time
    assert created["schedule"] is None


# ===========================================================================
# _detect_local_iana_timezone
# ===========================================================================


class TestDetectLocalIanaTimezone:
    def test_macos_symlink(self):
        """Resolves /etc/localtime → .../zoneinfo/Asia/Shanghai."""
        fake_path = Path("/var/db/timezone/zoneinfo/Asia/Shanghai")
        with patch.object(Path, "resolve", return_value=fake_path):
            assert _detect_local_iana_timezone() == "Asia/Shanghai"

    def test_linux_symlink(self):
        """Resolves /etc/localtime → /usr/share/zoneinfo/US/Eastern."""
        fake_path = Path("/usr/share/zoneinfo/US/Eastern")
        with patch.object(Path, "resolve", return_value=fake_path):
            assert _detect_local_iana_timezone() == "US/Eastern"

    def test_zoneinfo_default_variant(self):
        """Handles zoneinfo.default or similar prefixed dirs."""
        fake_path = Path("/var/db/timezone/zoneinfo.default/Europe/London")
        with patch.object(Path, "resolve", return_value=fake_path):
            assert _detect_local_iana_timezone() == "Europe/London"

    def test_fallback_utc_offset(self):
        """Falls back to UTC offset when symlink has no zoneinfo component."""
        fake_path = Path("/some/random/path")
        with patch.object(Path, "resolve", return_value=fake_path):
            result = _detect_local_iana_timezone()
            # Should be a UTC+HH:MM or UTC-HH:MM string
            assert result.startswith("UTC")

    def test_all_fail_returns_utc(self):
        """Returns 'UTC' when everything fails."""
        with patch.object(Path, "resolve", side_effect=OSError("no file")):
            with patch("src.everbot.core.tasks.routine_manager.datetime") as mock_dt:
                mock_dt.now.side_effect = Exception("boom")
                assert _detect_local_iana_timezone() == "UTC"


# ===========================================================================
# add_routine default timezone
# ===========================================================================


def test_add_routine_defaults_timezone_when_schedule_set(tmp_path: Path):
    """When schedule is provided but timezone_name is empty, auto-detect timezone."""
    manager = RoutineManager(tmp_path)
    with patch(
        "src.everbot.core.tasks.routine_manager._detect_local_iana_timezone",
        return_value="Asia/Shanghai",
    ):
        created = manager.add_routine(
            title="Auto tz test",
            description="should get default tz",
            schedule="1h",
            now=datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc),
        )
    assert created["timezone"] == "Asia/Shanghai"


def test_add_routine_no_default_timezone_without_schedule(tmp_path: Path):
    """One-shot tasks (no schedule) should NOT get a default timezone."""
    manager = RoutineManager(tmp_path)
    created = manager.add_routine(
        title="One-shot no tz",
        description="no schedule, no tz",
        next_run_at="2026-03-01T09:00:00+00:00",
    )
    # timezone should remain unset (None or empty)
    assert not created.get("timezone")
