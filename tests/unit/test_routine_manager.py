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


def test_corrupted_json_with_control_chars_detected_as_corrupted(tmp_path: Path):
    """HEARTBEAT.md with control characters in JSON strings must be detected as corrupted.

    This reproduces the production bug where Chinese titles like "每日新闻简报生成"
    were written with wrong encoding, producing control characters (U+0000-U+001F)
    that cause json.loads to fail with 'Invalid control character'.
    """
    # Manually construct JSON with raw control characters in the title value.
    # json.dumps would escape these, but encoding corruption writes them raw.
    corrupted_json = (
        '{\n'
        '  "version": 2,\n'
        '  "tasks": [\n'
        '    {\n'
        '      "id": "routine_abc123",\n'
        '      "title": "\x01\x0f\x03\x17\x05\x06",\n'
        '      "schedule": "1h",\n'
        '      "state": "pending"\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    content = f"# HEARTBEAT\n\n## Tasks\n\n```json\n{corrupted_json}\n```\n"
    (tmp_path / "HEARTBEAT.md").write_text(content, encoding="utf-8")

    manager = RoutineManager(tmp_path)
    with pytest.raises(ValueError, match="corrupted"):
        manager.list_routines()


def test_invalid_utf8_bytes_in_heartbeat_handled_gracefully(tmp_path: Path):
    """HEARTBEAT.md with invalid UTF-8 bytes must not crash with UnicodeDecodeError.

    When a file-writing tool corrupts Chinese content encoding, the file may
    contain raw bytes that are not valid UTF-8. _read_content() uses
    encoding='utf-8' which would raise UnicodeDecodeError — this should be
    caught and reported as corruption, not an unhandled crash.
    """
    # Write raw bytes that are valid latin-1 but invalid UTF-8 sequences
    # This simulates a tool writing Chinese text with wrong encoding
    header = b"# HEARTBEAT\n\n## Tasks\n\n```json\n"
    # 0xE6 0xAF 0x8F is valid UTF-8 for '每', but truncate to make invalid UTF-8
    invalid_json = b'{"version": 2, "tasks": [{"id": "r1", "title": "\xe6\xaf"}]}'
    footer = b"\n```\n"
    (tmp_path / "HEARTBEAT.md").write_bytes(header + invalid_json + footer)

    manager = RoutineManager(tmp_path)
    # Currently this raises UnicodeDecodeError (unhandled bug).
    # The correct behavior would be ValueError("corrupted"), but we test
    # that it at least raises *something* rather than silently returning bad data.
    with pytest.raises((ValueError, UnicodeDecodeError)):
        manager.list_routines()


def test_chinese_roundtrip_through_add_and_list(tmp_path: Path):
    """Chinese titles and descriptions must survive add → persist → list round-trip."""
    manager = RoutineManager(tmp_path)
    created = manager.add_routine(
        title="每日新闻简报生成",
        description="学术论文发现与推送",
        schedule="1h",
        execution_mode="inline",
        now=datetime(2026, 2, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert created["title"] == "每日新闻简报生成"
    assert created["description"] == "学术论文发现与推送"

    # Re-read from disk and verify Chinese is preserved
    listed = manager.list_routines()
    assert len(listed) == 1
    assert listed[0]["title"] == "每日新闻简报生成"
    assert listed[0]["description"] == "学术论文发现与推送"

    # Also verify the raw file content is valid UTF-8 with correct Chinese
    raw = (tmp_path / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert "每日新闻简报生成" in raw
    assert "学术论文发现与推送" in raw


def test_chinese_roundtrip_through_parse_and_write(tmp_path: Path):
    """write_task_block + parse_heartbeat_md must preserve Chinese characters."""
    from src.everbot.core.tasks.task_manager import Task, TaskList, write_task_block

    task_list = TaskList(version=2, tasks=[
        Task(
            id="routine_test01",
            title="每日投资信号推送",
            description="每天早上推送投资吸引子信号",
            schedule="1d",
        ),
    ])
    content = write_task_block("# HEARTBEAT\n", task_list)

    # Verify raw content has Chinese, not escaped unicode
    assert "每日投资信号推送" in content
    assert "\\u" not in content  # ensure_ascii=False should prevent escaping

    # Verify round-trip parse
    parsed = parse_heartbeat_md(content)
    assert parsed.status == ParseStatus.OK
    assert parsed.task_list is not None
    assert parsed.task_list.tasks[0].title == "每日投资信号推送"
    assert parsed.task_list.tasks[0].description == "每天早上推送投资吸引子信号"


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


# ===========================================================================
# P0: HEARTBEAT.md corruption - file lock + .bak recovery
# ===========================================================================


def test_concurrent_save_does_not_corrupt(tmp_path: Path):
    """Concurrent _save_task_list calls must not corrupt HEARTBEAT.md.

    Reproduces the production bug where rapid CLI updates (3 calls in 24s)
    caused file corruption due to read-modify-write without a file lock.
    """
    import concurrent.futures

    manager = RoutineManager(tmp_path)
    # Seed with some tasks
    for i in range(3):
        manager.add_routine(
            title=f"Task {i}",
            schedule=f"{i + 1}h",
            execution_mode="inline",
            now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        )

    errors = []

    def update_task(task_idx: int, iteration: int):
        try:
            m = RoutineManager(tmp_path)
            tasks = m.list_routines()
            if task_idx < len(tasks):
                m.update_routine(
                    tasks[task_idx]["id"],
                    description=f"updated by thread {task_idx} iter {iteration}",
                )
        except Exception as exc:
            errors.append(str(exc))

    # Simulate concurrent updates from multiple processes
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for iteration in range(5):
            for task_idx in range(3):
                futures.append(pool.submit(update_task, task_idx, iteration))
        concurrent.futures.wait(futures)

    # After all concurrent writes, file must still be parseable
    final_manager = RoutineManager(tmp_path)
    routines = final_manager.list_routines()
    assert len(routines) == 3, f"Expected 3 routines, got {len(routines)}; errors: {errors}"


def test_corrupted_heartbeat_recovers_from_bak(tmp_path: Path):
    """When HEARTBEAT.md is corrupted, _load_task_list should auto-recover from .bak.

    Reproduces: after corruption, 95 heartbeats reported anomaly for 35 hours
    because there was no auto-recovery mechanism.
    """
    manager = RoutineManager(tmp_path)
    manager.add_routine(
        title="Recoverable task",
        schedule="1h",
        now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )

    # Verify the task is persisted
    hb_path = tmp_path / "HEARTBEAT.md"
    bak_path = tmp_path / "HEARTBEAT.md.bak"

    # Save a good copy as .bak (simulating atomic_save's rotation)
    good_content = hb_path.read_bytes()
    bak_path.write_bytes(good_content)

    # Now corrupt the main file
    hb_path.write_text("# HEARTBEAT\n```json\n{invalid corrupted\n```\n", encoding="utf-8")

    # _load_task_list should recover from .bak instead of raising
    recovered_manager = RoutineManager(tmp_path)
    routines = recovered_manager.list_routines()
    assert len(routines) == 1
    assert routines[0]["title"] == "Recoverable task"


def test_invalid_utf8_raises_valueerror_not_unicode_error(tmp_path: Path):
    """_read_content must raise ValueError (not UnicodeDecodeError) for invalid UTF-8.

    This strengthens test_invalid_utf8_bytes_in_heartbeat_handled_gracefully
    to require the correct exception type for proper error handling upstream.
    """
    header = b"# HEARTBEAT\n\n## Tasks\n\n```json\n"
    invalid_json = b'{"version": 2, "tasks": [{"id": "r1", "title": "\xe6\xaf"}]}'
    footer = b"\n```\n"
    (tmp_path / "HEARTBEAT.md").write_bytes(header + invalid_json + footer)

    manager = RoutineManager(tmp_path)
    with pytest.raises(ValueError):
        manager.list_routines()


def test_min_execution_interval_validation(tmp_path: Path):
    """Invalid min_execution_interval format must be rejected."""
    manager = RoutineManager(tmp_path)
    with pytest.raises(ValueError, match="Invalid min_execution_interval"):
        manager.add_routine(
            title="Bad interval",
            schedule="1h",
            min_execution_interval="abc",
        )

    # Valid formats should work
    created = manager.add_routine(
        title="Good interval",
        schedule="1h",
        skill="memory-review",
        scanner="session",
        min_execution_interval="2h",
        now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert created["min_execution_interval"] == "2h"
    assert created["skill"] == "memory-review"
    assert created["scanner"] == "session"


# ===========================================================================
# High-frequency schedule constraint (< 30m requires skill + scanner)
# ===========================================================================


class TestHighFrequencyConstraint:
    """schedule < 30m without skill+scanner should be rejected."""

    @pytest.mark.parametrize("schedule", ["2m", "5m", "15m", "29m"])
    def test_rejects_high_frequency_without_skill_scanner(self, tmp_path, schedule):
        mgr = RoutineManager(tmp_path)
        with pytest.raises(ValueError, match="High-frequency"):
            mgr.add_routine(title=f"test {schedule}", schedule=schedule)

    def test_allows_high_frequency_with_skill_and_scanner(self, tmp_path):
        mgr = RoutineManager(tmp_path)
        result = mgr.add_routine(
            title="test 2m with gate",
            schedule="2m",
            skill="memory-review",
            scanner="session",
        )
        assert result["skill"] == "memory-review"
        assert result["scanner"] == "session"

    def test_allows_30m_boundary(self, tmp_path):
        mgr = RoutineManager(tmp_path)
        result = mgr.add_routine(title="test 30m", schedule="30m")
        assert result["schedule"] == "30m"

    @pytest.mark.parametrize("schedule", ["1h", "2h", "1d"])
    def test_allows_low_frequency(self, tmp_path, schedule):
        mgr = RoutineManager(tmp_path)
        result = mgr.add_routine(title=f"test {schedule}", schedule=schedule)
        assert result["schedule"] == schedule

    def test_cron_expression_not_affected(self, tmp_path):
        mgr = RoutineManager(tmp_path)
        result = mgr.add_routine(title="test cron", schedule="*/5 * * * *")
        assert result["schedule"] == "*/5 * * * *"

    def test_rejects_skill_without_scanner(self, tmp_path):
        mgr = RoutineManager(tmp_path)
        with pytest.raises(ValueError, match="High-frequency"):
            mgr.add_routine(title="test", schedule="5m", skill="memory-review")

    def test_rejects_scanner_without_skill(self, tmp_path):
        mgr = RoutineManager(tmp_path)
        with pytest.raises(ValueError, match="High-frequency"):
            mgr.add_routine(title="test", schedule="5m", scanner="session")
