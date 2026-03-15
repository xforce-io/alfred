"""Unit tests for IntegrationFailureClassifier."""

import json
import sys
from pathlib import Path

import pytest

# Ensure scripts dir is importable
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from integration_failure_classifier import FailureType, IntegrationFailureClassifier


# ── Fixtures ──


def _make_report(**overrides):
    base = {
        "created_at": "2026-03-15T00:00:00+00:00",
        "dev_branch": "dev/test",
        "merge_order": ["1"],
        "merge_results": [],
        "overall": "failed",
    }
    base.update(overrides)
    return base


# ── Classification tests ──


class TestClassify:
    def test_merge_conflict(self):
        report = _make_report(
            failure_type="merge_conflict",
            failed_feature="2",
            failed_branch="feat/2-thing",
            error="CONFLICT (content): Merge conflict in src/main.py",
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.MERGE_CONFLICT

    def test_test_failure(self):
        report = _make_report(
            failure_type="test_failure",
            all_merged=True,
            test={"passed": False, "output": "FAIL: test_xyz\nAssertionError"},
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.TEST_FAILURE

    def test_filesystem_error_in_test_output(self):
        report = _make_report(
            failure_type="test_failure",
            test={"passed": False, "output": "PermissionError: [Errno 13] Permission denied: '/tmp/x'"},
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.FILESYSTEM_ERROR

    def test_filesystem_error_no_space(self):
        report = _make_report(
            error="OSError: No space left on device",
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.FILESYSTEM_ERROR

    def test_filesystem_error_read_only(self):
        report = _make_report(
            error="Read-only file system",
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.FILESYSTEM_ERROR

    def test_unknown_error(self):
        report = _make_report(
            error="Something completely unexpected happened",
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.UNKNOWN

    def test_passed_report_raises(self):
        report = _make_report(overall="passed")
        c = IntegrationFailureClassifier(report)
        with pytest.raises(ValueError, match="success"):
            c.classify()

    def test_conflict_keyword_in_error_without_failure_type(self):
        report = _make_report(
            error="CONFLICT (content): Merge conflict in foo.py",
        )
        c = IntegrationFailureClassifier(report)
        assert c.classify() == FailureType.MERGE_CONFLICT


# ── Repair suggestion tests ──


class TestRepairSuggestion:
    def test_merge_conflict_suggestion_has_feature(self):
        report = _make_report(
            failure_type="merge_conflict",
            failed_feature="3",
            failed_branch="feat/3-bar",
        )
        c = IntegrationFailureClassifier(report)
        suggestion = c.get_repair_suggestion()
        assert suggestion["failure_type"] == "merge_conflict"
        assert suggestion["failed_feature"] == "3"
        assert suggestion["failed_branch"] == "feat/3-bar"
        assert "cm reopen" in suggestion["steps"]
        assert "<N>" not in suggestion["steps"]  # placeholder replaced

    def test_test_failure_suggestion_has_snippet(self):
        report = _make_report(
            failure_type="test_failure",
            test={"passed": False, "output": "FAIL: test_something\nExpected 1 got 2"},
        )
        c = IntegrationFailureClassifier(report)
        suggestion = c.get_repair_suggestion()
        assert suggestion["failure_type"] == "test_failure"
        assert "test_output_snippet" in suggestion
        assert "FAIL" in suggestion["test_output_snippet"]

    def test_filesystem_suggestion(self):
        report = _make_report(error="Permission denied: /foo")
        c = IntegrationFailureClassifier(report)
        suggestion = c.get_repair_suggestion()
        assert suggestion["failure_type"] == "filesystem_error"
        assert "df -h" in suggestion["steps"]

    def test_unknown_suggestion(self):
        report = _make_report(error="wat")
        c = IntegrationFailureClassifier(report)
        suggestion = c.get_repair_suggestion()
        assert suggestion["failure_type"] == "unknown"
        assert "cm doctor" in suggestion["steps"]


# ── Summary tests ──


class TestSummary:
    def test_summary_structure(self):
        report = _make_report(
            failure_type="test_failure",
            test={"passed": False, "output": "FAIL"},
        )
        c = IntegrationFailureClassifier(report)
        s = c.summary()
        assert s["failure_type"] == "test_failure"
        assert "suggestion" in s
        assert "steps" in s["suggestion"]


# ── from_file tests ──


class TestFromFile:
    def test_from_file(self, tmp_path):
        report = _make_report(failure_type="merge_conflict", failed_feature="1")
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        c = IntegrationFailureClassifier.from_file(p)
        assert c.classify() == FailureType.MERGE_CONFLICT
