#!/usr/bin/env python3
"""Test and lint execution with structured reports."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from config_manager import ConfigManager

CMD_TIMEOUT = 300
OUTPUT_MAX = 5000


@dataclass
class LintResult:
    passed: bool
    output: str


@dataclass
class TestResult:
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    output: str


@dataclass
class TestReport:
    lint: LintResult
    test: TestResult
    overall: str  # "passed" | "failed"

    def to_dict(self) -> dict:
        return {
            "lint": asdict(self.lint),
            "test": asdict(self.test),
            "overall": self.overall,
        }


class TestRunner:
    def __init__(self, config: ConfigManager | None = None):
        self.config = config or ConfigManager()

    def run(self, workspace_name: str) -> dict:
        ws = self.config.get_workspace(workspace_name)
        if ws is None:
            return {"ok": False, "error": f"workspace '{workspace_name}' not found"}

        ws_path = ws["path"]
        commands = self._detect_commands(ws_path, ws)

        lint_result = self._run_lint(ws_path, commands.get("lint_command"))
        test_result = self._run_test(ws_path, commands.get("test_command"))

        overall = "passed" if (lint_result.passed and test_result.passed) else "failed"
        report = TestReport(lint=lint_result, test=test_result, overall=overall)

        # Save artifact
        art_dir = Path(ws_path) / ".coding-master"
        art_dir.mkdir(exist_ok=True)
        report_path = art_dir / "test_report.json"
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
        )

        return {"ok": True, "data": report.to_dict()}

    def _detect_commands(self, ws_path: str, ws_config: dict) -> dict:
        test_cmd = ws_config.get("test_command")
        lint_cmd = ws_config.get("lint_command")
        p = Path(ws_path)

        if not test_cmd:
            if (p / "pyproject.toml").exists():
                test_cmd = "pytest"
            elif (p / "package.json").exists():
                test_cmd = "npm test"
            elif (p / "Cargo.toml").exists():
                test_cmd = "cargo test"

        if not lint_cmd:
            if (p / "pyproject.toml").exists():
                if _has_tool(p / "pyproject.toml", "ruff"):
                    lint_cmd = "ruff check ."
            elif (p / "package.json").exists():
                lint_cmd = "npm run lint"
            elif (p / "Cargo.toml").exists():
                lint_cmd = "cargo clippy"

        return {"test_command": test_cmd, "lint_command": lint_cmd}

    def _run_lint(self, ws_path: str, cmd: str | None) -> LintResult:
        if not cmd:
            return LintResult(passed=True, output="no lint command configured")
        stdout, stderr, rc = _exec(ws_path, cmd)
        output = _truncate(stdout + stderr, OUTPUT_MAX)
        return LintResult(passed=(rc == 0), output=output)

    def _run_test(self, ws_path: str, cmd: str | None) -> TestResult:
        if not cmd:
            return TestResult(
                passed=True, total=0, passed_count=0, failed_count=0,
                output="no test command configured",
            )
        stdout, stderr, rc = _exec(ws_path, cmd)
        output = _truncate(stdout + stderr, OUTPUT_MAX)
        total, passed, failed = _parse_pytest_output(stdout + stderr)
        return TestResult(
            passed=(rc == 0),
            total=total,
            passed_count=passed,
            failed_count=failed,
            output=output,
        )


def _exec(cwd: str, cmd: str) -> tuple[str, str, int]:
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"<timeout after {CMD_TIMEOUT}s>", 1
    except Exception as e:
        return "", str(e), 1


def _parse_pytest_output(text: str) -> tuple[int, int, int]:
    """Extract (total, passed, failed) from pytest summary line."""
    # "5 passed, 2 failed" or "42 passed" etc.
    passed = 0
    failed = 0
    m_passed = re.search(r"(\d+)\s+passed", text)
    m_failed = re.search(r"(\d+)\s+failed", text)
    if m_passed:
        passed = int(m_passed.group(1))
    if m_failed:
        failed = int(m_failed.group(1))
    total = passed + failed
    # also check for errors
    m_error = re.search(r"(\d+)\s+error", text)
    if m_error:
        total += int(m_error.group(1))
    return total, passed, failed


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n... (truncated)"


def _has_tool(path: Path, tool: str) -> bool:
    try:
        return f"[tool.{tool}]" in path.read_text()
    except Exception:
        return False
