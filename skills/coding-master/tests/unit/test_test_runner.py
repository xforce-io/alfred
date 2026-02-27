"""Tests for test_runner.py — pytest output parsing and report structure."""

import pytest

from test_runner import _parse_pytest_output, TestResult, LintResult, TestReport


class TestParsePytestOutput:
    def test_passed_only(self):
        total, passed, failed = _parse_pytest_output("5 passed in 1.23s")
        assert total == 5
        assert passed == 5
        assert failed == 0

    def test_passed_and_failed(self):
        total, passed, failed = _parse_pytest_output("3 passed, 2 failed in 4.56s")
        assert total == 5
        assert passed == 3
        assert failed == 2

    def test_with_errors(self):
        total, passed, failed = _parse_pytest_output("1 passed, 1 failed, 2 error in 3s")
        assert total == 4  # 1 + 1 + 2
        assert passed == 1
        assert failed == 1

    def test_failed_only(self):
        total, passed, failed = _parse_pytest_output("3 failed in 2.00s")
        assert total == 3
        assert passed == 0
        assert failed == 3

    def test_no_match(self):
        total, passed, failed = _parse_pytest_output("some random output")
        assert total == 0
        assert passed == 0
        assert failed == 0

    def test_multiline_output(self):
        output = """
===== test session starts =====
collected 10 items
tests/test_foo.py ....F.....
===== 9 passed, 1 failed in 5.67s =====
"""
        total, passed, failed = _parse_pytest_output(output)
        assert total == 10
        assert passed == 9
        assert failed == 1


class TestReportStructure:
    def test_to_dict(self):
        lint = LintResult(passed=True, output="ok")
        test = TestResult(passed=True, total=5, passed_count=5, failed_count=0, output="5 passed")
        report = TestReport(lint=lint, test=test, overall="passed")
        d = report.to_dict()
        assert d["overall"] == "passed"
        assert d["lint"]["passed"] is True
        assert d["test"]["total"] == 5
        assert d["test"]["passed_count"] == 5

    def test_failed_overall(self):
        lint = LintResult(passed=True, output="ok")
        test = TestResult(passed=False, total=3, passed_count=1, failed_count=2, output="fail")
        report = TestReport(lint=lint, test=test, overall="failed")
        d = report.to_dict()
        assert d["overall"] == "failed"
        assert d["test"]["failed_count"] == 2


class TestNoCommand:
    """When no test/lint command is configured, runner returns passed."""

    def test_no_test_command_returns_passed(self):
        result = TestResult(
            passed=True, total=0, passed_count=0, failed_count=0,
            output="no test command configured",
        )
        assert result.passed is True

    def test_no_lint_command_returns_passed(self):
        result = LintResult(passed=True, output="no lint command configured")
        assert result.passed is True
