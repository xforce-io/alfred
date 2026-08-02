"""Tests for TaskExecutionGate — unified guard logic for skill tasks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


from src.everbot.core.scanners.base import ScanResult
from src.everbot.core.scanners.reflection_state import ReflectionState
from src.everbot.core.tasks.execution_gate import GateVerdict, TaskExecutionGate


def _make_task(**overrides):
    defaults = {
        "id": "job_1",
        "title": "Job Task",
        "job": "test-skill",
        "scanner": "session",
        "min_execution_interval": None,
        "last_run_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── check() scenarios ──────────────────────────────────────────


class TestGateCheck:
    def test_no_scanner_passes(self, tmp_path: Path):
        """Task with no scanner type should pass gate (no scanner to consult)."""
        task = _make_task(scanner=None)
        gate = TaskExecutionGate(tmp_path, "agent", lambda t: None)
        verdict = gate.check(task)
        assert verdict.allowed is True
        assert verdict.scan_result is None

    def test_scanner_has_changes_passes(self, tmp_path: Path):
        """When scanner reports changes, gate should allow."""
        scan_result = ScanResult(has_changes=True, change_summary="1 new session")
        scanner = MagicMock()
        scanner.check.return_value = scan_result

        gate = TaskExecutionGate(tmp_path, "agent", lambda t: scanner)
        verdict = gate.check(_make_task())
        assert verdict.allowed is True
        assert verdict.scan_result is scan_result

    def test_scanner_no_changes_blocks(self, tmp_path: Path):
        """When scanner reports no changes, gate should block."""
        scan_result = ScanResult(has_changes=False, change_summary="No changes")
        scanner = MagicMock()
        scanner.check.return_value = scan_result

        gate = TaskExecutionGate(tmp_path, "agent", lambda t: scanner)
        verdict = gate.check(_make_task())
        assert verdict.allowed is False
        assert verdict.skip_reason == "no_changes"

    def test_scanner_error_blocks(self, tmp_path: Path):
        """When scanner raises, gate should block with scanner_error."""
        scanner = MagicMock()
        scanner.check.side_effect = RuntimeError("disk full")

        gate = TaskExecutionGate(tmp_path, "agent", lambda t: scanner)
        verdict = gate.check(_make_task())
        assert verdict.allowed is False
        assert verdict.skip_reason == "scanner_error"

    def test_min_interval_not_met_blocks(self, tmp_path: Path):
        """When min_execution_interval has not elapsed, gate should block."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        task = _make_task(
            scanner=None,
            min_execution_interval="2h",
            last_run_at=recent,
        )
        gate = TaskExecutionGate(tmp_path, "agent", lambda t: None)
        verdict = gate.check(task)
        assert verdict.allowed is False
        assert verdict.skip_reason == "interval_not_met"

    def test_min_interval_met_passes(self, tmp_path: Path):
        """When min_execution_interval has elapsed, gate should allow."""
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        task = _make_task(
            scanner=None,
            min_execution_interval="2h",
            last_run_at=old,
        )
        gate = TaskExecutionGate(tmp_path, "agent", lambda t: None)
        verdict = gate.check(task)
        assert verdict.allowed is True

    def test_scanner_passes_but_interval_blocks(self, tmp_path: Path):
        """Scanner has changes but min_interval not met → block."""
        scan_result = ScanResult(has_changes=True, change_summary="changes")
        scanner = MagicMock()
        scanner.check.return_value = scan_result

        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = _make_task(min_execution_interval="1h", last_run_at=recent)

        gate = TaskExecutionGate(tmp_path, "agent", lambda t: scanner)
        verdict = gate.check(task)
        assert verdict.allowed is False
        assert verdict.skip_reason == "interval_not_met"
        # scan_result should still be captured for event logging
        assert verdict.scan_result is scan_result


# ── commit() scenarios ─────────────────────────────────────────


class TestGateCommit:
    def test_commit_writes_watermark(self, tmp_path: Path):
        """commit() should write watermark to .reflection_state.json."""
        task = _make_task()
        verdict = GateVerdict(allowed=True, scan_result=ScanResult(has_changes=True, change_summary="x"))
        gate = TaskExecutionGate(tmp_path, "agent", lambda t: None)

        gate.commit(task, verdict)

        state = ReflectionState.load(tmp_path)
        wm = state.get_watermark("test-skill")
        assert wm != "", "Watermark should be set"

    def test_commit_uses_current_time(self, tmp_path: Path):
        """Watermark should be set to ~now, not to scan-time values."""
        task = _make_task()
        verdict = GateVerdict(allowed=True)
        gate = TaskExecutionGate(tmp_path, "agent", lambda t: None)

        before = datetime.now(timezone.utc).isoformat()
        gate.commit(task, verdict)
        after = datetime.now(timezone.utc).isoformat()

        state = ReflectionState.load(tmp_path)
        wm = state.get_watermark("test-skill")
        assert before <= wm <= after

    def test_commit_no_job_is_noop(self, tmp_path: Path):
        """commit() with no job name should not write anything."""
        task = _make_task(job=None)
        verdict = GateVerdict(allowed=True)
        gate = TaskExecutionGate(tmp_path, "agent", lambda t: None)

        gate.commit(task, verdict)

        state = ReflectionState.load(tmp_path)
        assert state.get_watermark("") == ""

    def test_commit_preserves_watermark_advanced_by_job(self, tmp_path: Path):
        """A partial-batch job owns its exact analyzed-session boundary."""
        task = _make_task(job="memory-review")
        scanner = MagicMock()
        scanner.check.return_value = ScanResult(
            has_changes=True, change_summary="5 new sessions",
        )
        gate = TaskExecutionGate(tmp_path, "agent", lambda _type: scanner)
        verdict = gate.check(task)

        job_watermark = "2026-08-02T08:03:00+00:00"
        state = ReflectionState.load(tmp_path)
        state.set_watermark("memory-review", job_watermark)
        assert state.save(tmp_path)

        gate.commit(task, verdict)

        assert (
            ReflectionState.load(tmp_path).get_watermark("memory-review")
            == job_watermark
        )

    def test_commit_advances_when_job_left_watermark_unchanged(self, tmp_path: Path):
        task = _make_task(job="test-skill")
        initial = "2026-08-01T00:00:00+00:00"
        state = ReflectionState.load(tmp_path)
        state.set_watermark("test-skill", initial)
        assert state.save(tmp_path)
        scanner = MagicMock()
        scanner.check.return_value = ScanResult(
            has_changes=True, change_summary="new session",
        )
        gate = TaskExecutionGate(tmp_path, "agent", lambda _type: scanner)
        verdict = gate.check(task)

        gate.commit(task, verdict)

        assert ReflectionState.load(tmp_path).get_watermark("test-skill") > initial
