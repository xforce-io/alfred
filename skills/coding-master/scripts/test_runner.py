#!/usr/bin/env python3
"""Test and lint execution with structured reports."""

from __future__ import annotations

import json
import re
import shlex
import shutil
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
class EnvFingerprint:
    """Snapshot of the execution environment, included in every test report."""
    cwd: str
    python: str            # resolved path to python
    python_version: str
    pytest: str             # resolved path to pytest (or "")
    pip_available: bool
    package_manager: str    # "uv" | "pip" | "poetry" | "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TestReport:
    lint: LintResult
    test: TestResult
    overall: str  # "passed" | "failed"
    env: EnvFingerprint | None = None

    def to_dict(self) -> dict:
        d = {
            "lint": asdict(self.lint),
            "test": asdict(self.test),
            "overall": self.overall,
        }
        if self.env:
            d["env"] = self.env.to_dict()
        return d


class TestRunner:
    def __init__(self, config: ConfigManager | None = None):
        self.config = config or ConfigManager()

    def run(self, workspace_name: str) -> dict:
        ws = self.config.get_workspace(workspace_name)
        if ws is None:
            return {"ok": False, "error": f"workspace '{workspace_name}' not found"}

        ws_path = ws["path"]
        commands = self._detect_commands(ws_path, ws)
        env_fp = _probe_env(ws_path)

        lint_result = self._run_lint(ws_path, commands.get("lint_command"))
        test_result = self._run_test(ws_path, commands.get("test_command"))

        overall = "passed" if (lint_result.passed and test_result.passed) else "failed"
        report = TestReport(lint=lint_result, test=test_result, overall=overall, env=env_fp)

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
                test_cmd = _resolve_pytest_command(p)
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
            shlex.split(cmd), shell=False, cwd=cwd,
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
    # also check for errors (collection errors, etc.) — count as failures
    m_error = re.search(r"(\d+)\s+error", text)
    if m_error:
        failed += int(m_error.group(1))
    total = passed + failed
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


# ── Pytest command resolution ──────────────────────────────

def _find_venv_binary(project_path: Path, binary: str) -> Path | None:
    """Find a binary in .venv/bin, falling back to the git main worktree.

    Git worktrees share the code but not .venv.  When running inside a
    worktree (session or feature), look for .venv in the main repo first.
    """
    local = project_path / ".venv" / "bin" / binary
    if local.is_file():
        return local
    # Fall back to the main worktree (git worktree list --porcelain)
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                main_repo = Path(line.split(" ", 1)[1])
                candidate = main_repo / ".venv" / "bin" / binary
                if candidate.is_file():
                    return candidate
                break  # first entry is always the main worktree
    except Exception:
        pass
    return None


def _resolve_pytest_command(project_path: Path) -> str:
    """Pick the best way to invoke pytest for a Python project.

    Priority:
    1. .venv/bin/pytest (local or main worktree — works regardless of pip)
    2. uv run pytest   (if uv project detected)
    3. pytest           (bare — relies on PATH)
    """
    venv_pytest = _find_venv_binary(project_path, "pytest")
    if venv_pytest:
        return str(venv_pytest.resolve())

    if _is_uv_project(project_path):
        return "uv run pytest"

    return "pytest"


def _is_uv_project(project_path: Path) -> bool:
    """Detect a uv-managed project (uv.lock or uv-created venv)."""
    if (project_path / "uv.lock").exists():
        return True
    cfg = project_path / ".venv" / "pyvenv.cfg"
    if cfg.exists():
        try:
            return any(
                line.strip().startswith("uv")
                for line in cfg.read_text().splitlines()
            )
        except Exception:
            pass
    return False


# ── Environment fingerprint ────────────────────────────────

def _probe_env(ws_path: str) -> EnvFingerprint:
    """Capture a snapshot of python/pytest/pip in the workspace."""
    p = Path(ws_path)

    # Resolve python
    venv_python = p / ".venv" / "bin" / "python"
    if venv_python.is_file():
        python_path = str(venv_python.resolve())
    else:
        python_path = shutil.which("python3") or shutil.which("python") or ""

    # Python version
    py_version = ""
    if python_path:
        try:
            r = subprocess.run(
                [python_path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            py_version = r.stdout.strip() or r.stderr.strip()
        except Exception:
            pass

    # Resolve pytest
    venv_pytest = p / ".venv" / "bin" / "pytest"
    if venv_pytest.is_file():
        pytest_path = str(venv_pytest.resolve())
    else:
        pytest_path = shutil.which("pytest") or ""

    # pip available?
    pip_available = (p / ".venv" / "bin" / "pip").is_file()
    if not pip_available and python_path:
        try:
            r = subprocess.run(
                [python_path, "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            pip_available = r.returncode == 0
        except Exception:
            pass

    # Package manager
    pm = "unknown"
    if _is_uv_project(p):
        pm = "uv"
    elif (p / "poetry.lock").exists():
        pm = "poetry"
    elif (p / "Pipfile.lock").exists():
        pm = "pipenv"
    elif pip_available:
        pm = "pip"

    return EnvFingerprint(
        cwd=ws_path,
        python=python_path,
        python_version=py_version,
        pytest=pytest_path,
        pip_available=pip_available,
        package_manager=pm,
    )
