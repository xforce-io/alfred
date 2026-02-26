"""End-to-end integration test for the coding-master skill workflow.

Exercises the full Phase 0→7 pipeline using a real temporary git repo.
Only mocks what cannot run locally: EnvProber.probe/verify, ClaudeRunner.run,
and git push / gh pr create (no remote).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

# ── path setup (same as conftest.py) ──────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager
from workspace import WorkspaceManager, LockFile, ARTIFACT_DIR
from env_probe import EnvProber
from test_runner import TestRunner
from git_ops import GitOps
from engine import EngineResult
from engine.claude_runner import ClaudeRunner

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CALCULATOR_PY_BUGGY = textwrap.dedent("""\
    \"\"\"Simple calculator module (has a bug in multiply).\"\"\"


    def add(a: int, b: int) -> int:
        return a + b


    def multiply(a: int, b: int) -> int:
        return a + b  # BUG: should be a * b
""")

CALCULATOR_PY_FIXED = textwrap.dedent("""\
    \"\"\"Simple calculator module (has a bug in multiply).\"\"\"


    def add(a: int, b: int) -> int:
        return a + b


    def multiply(a: int, b: int) -> int:
        return a * b
""")

TEST_CALCULATOR_PY = textwrap.dedent("""\
    from calculator import add, multiply


    def test_add():
        assert add(2, 3) == 5


    def test_multiply():
        assert multiply(3, 4) == 12
""")

PYPROJECT_TOML = textwrap.dedent("""\
    [project]
    name = "calculator"
    version = "0.1.0"

    [tool.pytest.ini_options]
    testpaths = ["."]
""")


@pytest.fixture
def project(tmp_path):
    """Create a real git repo with a buggy calculator project."""
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "calculator.py").write_text(CALCULATOR_PY_BUGGY)
    (repo / "test_calculator.py").write_text(TEST_CALCULATOR_PY)
    (repo / "pyproject.toml").write_text(PYPROJECT_TOML)

    # Init real git repo
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")

    # Config file
    cfg_path = tmp_path / "config.yaml"
    data = {
        "coding_master": {
            "workspaces": {
                "test-ws": str(repo),
            },
            "envs": {
                "test-ws-local": str(repo),
            },
            "default_engine": "claude",
            "max_turns": 5,
        }
    }
    cfg_path.write_text(yaml.dump(data))

    return {
        "repo": repo,
        "cfg_path": cfg_path,
        "config": ConfigManager(config_path=cfg_path),
    }


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  E2E test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestE2EWorkflow:
    """Full workflow: workspace-check → env-probe → analyze → develop → test → submit-pr → env-verify."""

    TASK = "fix: multiply function uses + instead of *"

    # ── Phase 0: workspace-check (real) ──────────────────

    def test_phase0_workspace_check(self, project):
        """Real workspace-check: acquires lock, creates snapshot."""
        config = project["config"]
        repo = project["repo"]

        mgr = WorkspaceManager(config)
        result = mgr.check_and_acquire("test-ws", self.TASK, "claude")

        assert result["ok"] is True
        snapshot = result["data"]["snapshot"]

        # Lock file created
        lock = LockFile(str(repo))
        assert lock.exists()
        lock.load()
        assert lock.data["task"] == self.TASK
        assert lock.data["phase"] == "workspace-check"
        assert lock.data["engine"] == "claude"

        # Workspace snapshot saved
        snap_path = repo / ARTIFACT_DIR / "workspace_snapshot.json"
        assert snap_path.exists()
        saved = json.loads(snap_path.read_text())
        assert saved["git"]["branch"] == "main"
        # .gitignore was added by check_and_acquire, so the snapshot sees dirty=True
        # This is expected — the gitignore entry is an artifact of the workflow
        assert saved["runtime"]["type"] == "python"
        assert saved["project"]["test_command"] == "pytest"

    # ── Phase 1: env-probe (mock EnvProber.probe) ────────

    def test_phase1_env_probe(self, project):
        """Mock EnvProber.probe returns snapshot with errors."""
        config = project["config"]
        repo = project["repo"]

        # Phase 0 first
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")

        fake_snapshot = {
            "env": {"name": "test-ws-local", "type": "local", "connect": str(repo)},
            "probed_at": "2026-02-25T10:00:00+00:00",
            "modules": [
                {
                    "name": "calculator",
                    "path": str(repo),
                    "process": {"running": True, "count": 1},
                    "log_tail": "...\nERROR: multiply(3,4) returned 7, expected 12",
                    "recent_errors": [
                        "ERROR: multiply(3,4) returned 7, expected 12",
                    ],
                }
            ],
            "uptime": "up 1 day",
            "disk_usage": "/dev/sda1 50G 20G 30G 40% /",
            "custom_probes": {},
        }

        with patch.object(EnvProber, "probe", return_value={"ok": True, "data": fake_snapshot}):
            prober = EnvProber(config)
            probe_result = prober.probe("test-ws-local")

        assert probe_result["ok"] is True

        # Save artifact (mimic cmd_env_probe)
        art_dir = repo / ARTIFACT_DIR
        art_dir.mkdir(exist_ok=True)
        snap_path = art_dir / "env_snapshot.json"
        snap_path.write_text(json.dumps(probe_result["data"], indent=2))

        lock = LockFile(str(repo))
        lock.load()
        lock.add_artifact("env_snapshot", f"{ARTIFACT_DIR}/env_snapshot.json")
        lock.update_phase("env-probe")
        lock.renew_lease()
        lock.save()

        # Verify artifact
        assert snap_path.exists()
        saved = json.loads(snap_path.read_text())
        assert len(saved["modules"]) == 1
        assert "multiply" in saved["modules"][0]["recent_errors"][0]

        # Verify lock
        lock.load()
        assert lock.data["phase"] == "env-probe"
        assert "env_snapshot" in lock.data["artifacts"]

    # ── Phase 2: analyze (mock ClaudeRunner.run) ─────────

    def test_phase2_analyze(self, project):
        """Mock engine returns analysis report."""
        config = project["config"]
        repo = project["repo"]

        # Setup: phase 0 + 1
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")
        self._setup_env_snapshot(repo)

        fake_analysis = EngineResult(
            success=True,
            summary=textwrap.dedent("""\
                ## Analysis

                **Problem location**: `calculator.py:9` — `multiply` function
                **Root cause**: Uses `+` operator instead of `*`
                **Fix proposal**: Change `return a + b` to `return a * b`
                **Impact**: Low — isolated function
                **Risk**: Low
            """),
            files_changed=[],
        )

        with patch.object(ClaudeRunner, "run", return_value=fake_analysis):
            engine = ClaudeRunner()
            result = engine.run(str(repo), "analyze prompt", max_turns=5)

        assert result.success is True

        # Save artifact (mimic cmd_analyze)
        art_dir = repo / ARTIFACT_DIR
        art_dir.mkdir(exist_ok=True)
        analysis_path = art_dir / "phase2_analysis.md"
        analysis_path.write_text(result.summary)

        lock = LockFile(str(repo))
        lock.load()
        lock.add_artifact("analysis_report", f"{ARTIFACT_DIR}/phase2_analysis.md")
        lock.update_phase("analyzing")
        lock.renew_lease()
        lock.save()

        # Verify
        assert analysis_path.exists()
        content = analysis_path.read_text()
        assert "multiply" in content
        assert "a + b" in content or "+" in content

        lock.load()
        assert lock.data["phase"] == "analyzing"

    # ── Phase 4: develop (mock engine + manual fix) ──────

    def test_phase4_develop(self, project):
        """Mock engine, then manually fix the bug to simulate coding."""
        config = project["config"]
        repo = project["repo"]

        # Setup: phase 0 → 2
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")
        self._setup_env_snapshot(repo)
        self._setup_analysis(repo)

        # Create feature branch (real git)
        git = GitOps(str(repo))
        br_result = git.create_branch("fix/multiply-bug")
        assert br_result["ok"] is True
        assert br_result["data"]["branch"] == "fix/multiply-bug"

        lock = LockFile(str(repo))
        lock.load()
        lock.data["branch"] = "fix/multiply-bug"
        lock.save()

        # Simulate engine fixing the file
        (repo / "calculator.py").write_text(CALCULATOR_PY_FIXED)

        fake_develop = EngineResult(
            success=True,
            summary="Fixed multiply: changed + to * in calculator.py:9",
            files_changed=["calculator.py"],
        )

        with patch.object(ClaudeRunner, "run", return_value=fake_develop):
            engine = ClaudeRunner()
            result = engine.run(str(repo), "develop prompt", max_turns=5)

        assert result.success is True

        # Update lock (mimic cmd_develop)
        lock.load()
        lock.update_phase("developing")
        lock.renew_lease()
        lock.save()

        # Verify the fix is actually in place
        code = (repo / "calculator.py").read_text()
        assert "a * b" in code
        # The multiply function body (after its def line) should use * not +
        after_def = code[code.index("def multiply"):]
        return_line = [l for l in after_def.splitlines() if "return" in l][0]
        assert "*" in return_line
        assert "+" not in return_line

        lock.load()
        assert lock.data["phase"] == "developing"
        assert lock.data["branch"] == "fix/multiply-bug"

    # ── Phase 5: test (real pytest) ──────────────────────

    def test_phase5_test(self, project):
        """Real pytest execution — tests should pass after fix."""
        config = project["config"]
        repo = project["repo"]

        # Setup: phase 0 → 4 (including the fix)
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")
        self._setup_env_snapshot(repo)
        self._setup_analysis(repo)

        # Apply fix
        (repo / "calculator.py").write_text(CALCULATOR_PY_FIXED)

        # Real test run
        runner = TestRunner(config)
        result = runner.run("test-ws")

        assert result["ok"] is True
        report = result["data"]
        assert report["overall"] == "passed"
        assert report["test"]["passed"] is True
        assert report["test"]["passed_count"] == 2
        assert report["test"]["failed_count"] == 0

        # Verify artifact saved
        report_path = repo / ARTIFACT_DIR / "test_report.json"
        assert report_path.exists()

        # Update lock
        lock = LockFile(str(repo))
        lock.load()
        lock.add_artifact("test_report", f"{ARTIFACT_DIR}/test_report.json")
        lock.update_phase("testing")
        lock.renew_lease()
        lock.save()

        lock.load()
        assert lock.data["phase"] == "testing"

    # ── Phase 5 negative: test before fix (should fail) ──

    def test_phase5_test_before_fix(self, project):
        """Real pytest on buggy code — should fail."""
        config = project["config"]
        repo = project["repo"]

        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")

        runner = TestRunner(config)
        result = runner.run("test-ws")

        assert result["ok"] is True  # runner itself succeeds
        report = result["data"]
        assert report["overall"] == "failed"
        assert report["test"]["passed"] is False
        assert report["test"]["failed_count"] >= 1

    # ── Phase 6: submit-pr (mock push + gh) ──────────────

    def test_phase6_submit_pr(self, project):
        """Mock git push and gh pr create — verify lock state."""
        config = project["config"]
        repo = project["repo"]

        # Setup: phase 0 → 5
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")
        self._setup_env_snapshot(repo)
        self._setup_analysis(repo)

        # Create branch + fix
        git = GitOps(str(repo))
        git.create_branch("fix/multiply-bug")
        (repo / "calculator.py").write_text(CALCULATOR_PY_FIXED)

        lock = LockFile(str(repo))
        lock.load()
        lock.data["branch"] = "fix/multiply-bug"
        lock.update_phase("testing")
        lock.renew_lease()
        lock.save()

        # Mock push and gh pr create
        original_run = subprocess.run

        def mock_subprocess_run(cmd, **kwargs):
            cmd_list = cmd if isinstance(cmd, list) else [cmd]
            cmd_str = " ".join(str(c) for c in cmd_list)

            # Mock git push
            if "git" in cmd_str and "push" in cmd_str:
                mock_result = MagicMock()
                mock_result.stdout = ""
                mock_result.stderr = ""
                mock_result.returncode = 0
                return mock_result

            # Mock gh pr create
            if "gh" in cmd_str and "pr" in cmd_str:
                mock_result = MagicMock()
                mock_result.stdout = "https://github.com/test/repo/pull/42"
                mock_result.stderr = ""
                mock_result.returncode = 0
                return mock_result

            # Everything else is real
            return original_run(cmd, **kwargs)

        with patch("git_ops.subprocess.run", side_effect=mock_subprocess_run):
            result = git.submit_pr(
                title="fix: multiply uses + instead of *",
                body="Fixed the multiply function in calculator.py",
                commit_message="fix: multiply uses + instead of *",
            )

        assert result["ok"] is True
        assert "pull/42" in result["data"]["pr_url"]

        # Update lock (mimic cmd_submit_pr)
        lock.load()
        lock.data["pushed_to_remote"] = True
        lock.update_phase("submitted")
        lock.renew_lease()
        lock.save()

        lock.load()
        assert lock.data["phase"] == "submitted"
        assert lock.data["pushed_to_remote"] is True

    # ── Phase 7: env-verify (mock probe) ─────────────────

    def test_phase7_env_verify(self, project):
        """Mock probe returns no errors — issue resolved."""
        config = project["config"]
        repo = project["repo"]

        # Setup: phase 0 + env snapshot (baseline with errors)
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire("test-ws", self.TASK, "claude")
        self._setup_env_snapshot(repo)

        lock = LockFile(str(repo))
        lock.load()
        lock.update_phase("submitted")
        lock.renew_lease()
        lock.save()

        # Baseline has errors (from Phase 1)
        baseline_path = repo / ARTIFACT_DIR / "env_snapshot.json"
        assert baseline_path.exists()

        # Current probe returns no errors
        clean_snapshot = {
            "env": {"name": "test-ws-local", "type": "local", "connect": str(repo)},
            "probed_at": "2026-02-25T12:00:00+00:00",
            "modules": [
                {
                    "name": "calculator",
                    "path": str(repo),
                    "process": {"running": True, "count": 1},
                    "log_tail": "...\nINFO: all tests passing",
                    "recent_errors": [],
                }
            ],
            "uptime": "up 1 day",
            "disk_usage": "/dev/sda1 50G 20G 30G 40% /",
            "custom_probes": {},
        }

        with patch.object(
            EnvProber, "probe", return_value={"ok": True, "data": clean_snapshot}
        ):
            prober = EnvProber(config)
            result = prober.verify("test-ws-local", str(baseline_path))

        assert result["ok"] is True
        report = result["data"]
        assert report["resolved"] is True
        assert len(report["resolved_errors"]) == 1
        assert "multiply" in report["resolved_errors"][0]
        assert len(report["remaining_errors"]) == 0
        assert len(report["new_errors"]) == 0

        # Save artifact (mimic cmd_env_verify)
        verify_path = repo / ARTIFACT_DIR / "env_verify_report.json"
        verify_path.write_text(json.dumps(report, indent=2))

        lock.load()
        lock.add_artifact("env_verify_report", f"{ARTIFACT_DIR}/env_verify_report.json")
        lock.update_phase("env-verified")
        lock.renew_lease()
        lock.save()

        lock.load()
        assert lock.data["phase"] == "env-verified"
        assert "env_verify_report" in lock.data["artifacts"]

    # ── Full pipeline: Phase 0 → 7 in sequence ──────────

    def test_full_pipeline(self, project):
        """Run all phases in sequence, verifying the complete workflow."""
        config = project["config"]
        repo = project["repo"]

        # Phase 0: workspace-check (real)
        mgr = WorkspaceManager(config)
        r0 = mgr.check_and_acquire("test-ws", self.TASK, "claude")
        assert r0["ok"] is True

        # Phase 1: env-probe (mock)
        env_data = self._make_env_snapshot_data(repo, with_errors=True)
        with patch.object(EnvProber, "probe", return_value={"ok": True, "data": env_data}):
            prober = EnvProber(config)
            r1 = prober.probe("test-ws-local")
        assert r1["ok"] is True
        self._save_artifact(repo, "env_snapshot.json", r1["data"])
        self._advance_lock(repo, "env-probe", {"env_snapshot": f"{ARTIFACT_DIR}/env_snapshot.json"})

        # Phase 2: analyze (mock engine)
        fake_analysis = EngineResult(
            success=True,
            summary="multiply uses + instead of *. Fix: change + to * on line 9.",
            files_changed=[],
        )
        with patch.object(ClaudeRunner, "run", return_value=fake_analysis):
            engine = ClaudeRunner()
            r2 = engine.run(str(repo), "analyze", max_turns=5)
        assert r2.success is True
        self._save_artifact(repo, "phase2_analysis.md", r2.summary, is_text=True)
        self._advance_lock(repo, "analyzing", {"analysis_report": f"{ARTIFACT_DIR}/phase2_analysis.md"})

        # Phase 4: develop (mock engine + manual fix)
        git = GitOps(str(repo))
        git.create_branch("fix/multiply-bug")
        (repo / "calculator.py").write_text(CALCULATOR_PY_FIXED)

        lock = LockFile(str(repo))
        lock.load()
        lock.data["branch"] = "fix/multiply-bug"
        lock.save()
        self._advance_lock(repo, "developing")

        # Phase 5: test (real pytest)
        runner = TestRunner(config)
        r5 = runner.run("test-ws")
        assert r5["ok"] is True
        assert r5["data"]["overall"] == "passed"
        assert r5["data"]["test"]["passed_count"] == 2
        self._advance_lock(repo, "testing", {"test_report": f"{ARTIFACT_DIR}/test_report.json"})

        # Phase 6: submit-pr (mock push + gh)
        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in (cmd if isinstance(cmd, list) else [cmd]))
            if ("git" in cmd_str and "push" in cmd_str) or ("gh" in cmd_str and "pr" in cmd_str):
                m = MagicMock()
                m.stdout = "https://github.com/test/repo/pull/99" if "gh" in cmd_str else ""
                m.stderr = ""
                m.returncode = 0
                return m
            return original_run(cmd, **kwargs)

        with patch("git_ops.subprocess.run", side_effect=mock_run):
            r6 = git.submit_pr(
                title="fix: multiply bug",
                body="Changed + to * in multiply()",
                commit_message="fix: multiply bug",
            )
        assert r6["ok"] is True

        lock.load()
        lock.data["pushed_to_remote"] = True
        lock.save()
        self._advance_lock(repo, "submitted")

        # Phase 7: env-verify (mock probe — no errors)
        clean_data = self._make_env_snapshot_data(repo, with_errors=False)
        with patch.object(EnvProber, "probe", return_value={"ok": True, "data": clean_data}):
            prober = EnvProber(config)
            r7 = prober.verify("test-ws-local", str(repo / ARTIFACT_DIR / "env_snapshot.json"))
        assert r7["ok"] is True
        assert r7["data"]["resolved"] is True

        self._save_artifact(repo, "env_verify_report.json", r7["data"])
        self._advance_lock(repo, "env-verified", {"env_verify_report": f"{ARTIFACT_DIR}/env_verify_report.json"})

        # Final lock state check
        lock.load()
        assert lock.data["phase"] == "env-verified"
        assert lock.data["pushed_to_remote"] is True
        assert lock.data["branch"] == "fix/multiply-bug"
        assert set(lock.data["artifacts"].keys()) == {
            "workspace_snapshot", "session", "env_snapshot", "analysis_report",
            "test_report", "env_verify_report",
        }
        # Phase history should have all transitions
        phases_completed = [h["phase"] for h in lock.data["phase_history"]]
        assert "workspace-check" in phases_completed
        assert "env-probe" in phases_completed
        assert "analyzing" in phases_completed
        assert "developing" in phases_completed
        assert "testing" in phases_completed
        assert "submitted" in phases_completed

    # ── Helpers ──────────────────────────────────────────

    def _setup_env_snapshot(self, repo: Path) -> None:
        """Write a pre-canned env snapshot (baseline with errors)."""
        data = self._make_env_snapshot_data(repo, with_errors=True)
        self._save_artifact(repo, "env_snapshot.json", data)

        lock = LockFile(str(repo))
        lock.load()
        lock.add_artifact("env_snapshot", f"{ARTIFACT_DIR}/env_snapshot.json")
        lock.update_phase("env-probe")
        lock.renew_lease()
        lock.save()

    def _setup_analysis(self, repo: Path) -> None:
        """Write a pre-canned analysis report."""
        analysis = "## Analysis\n\nmultiply uses + instead of *. Fix line 9 of calculator.py."
        self._save_artifact(repo, "phase2_analysis.md", analysis, is_text=True)

        lock = LockFile(str(repo))
        lock.load()
        lock.add_artifact("analysis_report", f"{ARTIFACT_DIR}/phase2_analysis.md")
        lock.update_phase("analyzing")
        lock.renew_lease()
        lock.save()

    def _make_env_snapshot_data(self, repo: Path, with_errors: bool) -> dict:
        errors = (
            ["ERROR: multiply(3,4) returned 7, expected 12"]
            if with_errors
            else []
        )
        return {
            "env": {"name": "test-ws-local", "type": "local", "connect": str(repo)},
            "probed_at": "2026-02-25T10:00:00+00:00",
            "modules": [
                {
                    "name": "calculator",
                    "path": str(repo),
                    "process": {"running": True, "count": 1},
                    "log_tail": "",
                    "recent_errors": errors,
                }
            ],
            "uptime": "up 1 day",
            "disk_usage": "/dev/sda1 50G 20G 30G 40% /",
            "custom_probes": {},
        }

    @staticmethod
    def _save_artifact(repo: Path, filename: str, data, is_text: bool = False) -> None:
        art_dir = repo / ARTIFACT_DIR
        art_dir.mkdir(exist_ok=True)
        path = art_dir / filename
        if is_text:
            path.write_text(data)
        else:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    @staticmethod
    def _advance_lock(repo: Path, phase: str, artifacts: dict | None = None) -> None:
        lock = LockFile(str(repo))
        lock.load()
        lock.update_phase(phase)
        if artifacts:
            for k, v in artifacts.items():
                lock.add_artifact(k, v)
        lock.renew_lease()
        lock.save()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Repo-based E2E test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestE2ERepoWorkflow:
    """Repo-based workspace-check: clone into workspace slot, probe, snapshot."""

    def test_repo_workspace_check(self, tmp_path):
        """Register a bare repo, workspace-check with --repos, verify clone + snapshot."""
        # Create a bare repo with a calculator project
        origin = tmp_path / "origin"
        origin.mkdir()
        _git(origin, "init", "--bare")

        # Push initial commit via temp clone
        init_clone = tmp_path / "init_clone"
        subprocess.run(
            ["git", "clone", str(origin), str(init_clone)],
            capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        (init_clone / "calculator.py").write_text(CALCULATOR_PY_BUGGY)
        (init_clone / "test_calculator.py").write_text(TEST_CALCULATOR_PY)
        (init_clone / "pyproject.toml").write_text(PYPROJECT_TOML)
        _git(init_clone, "add", "-A")
        _git(init_clone, "commit", "-m", "initial")
        _git(init_clone, "push")

        # Setup config: repo + workspace slot
        ws_dir = tmp_path / "workspaces" / "env0"
        ws_dir.mkdir(parents=True)

        cfg_path = tmp_path / "config.yaml"
        data = {
            "coding_master": {
                "repos": {"calc": str(origin)},
                "workspaces": {"env0": str(ws_dir)},
                "envs": {},
                "default_engine": "claude",
                "max_turns": 5,
            }
        }
        cfg_path.write_text(yaml.dump(data))
        config = ConfigManager(config_path=cfg_path)

        # Phase 0: workspace-check with repos
        mgr = WorkspaceManager(config)
        result = mgr.check_and_acquire_for_repos(
            ["calc"], "fix multiply bug", "claude"
        )

        assert result["ok"] is True
        snapshot = result["data"]["snapshot"]

        # Workspace assigned
        assert snapshot["workspace"]["name"] == "env0"

        # Repo cloned and probed
        assert len(snapshot["repos"]) == 1
        repo_info = snapshot["repos"][0]
        assert repo_info["name"] == "calc"
        assert repo_info["git"]["branch"] is not None
        assert repo_info["runtime"]["type"] == "python"
        assert repo_info["project"]["test_command"] == "pytest"

        # primary_repo set
        assert snapshot["primary_repo"]["name"] == "calc"

        # Lock file created in workspace
        lock = LockFile(str(ws_dir))
        assert lock.exists()
        lock.load()
        assert lock.data["task"] == "fix multiply bug"

        # Snapshot artifact saved
        snap_path = ws_dir / ARTIFACT_DIR / "workspace_snapshot.json"
        assert snap_path.exists()

        # Repo actually cloned
        cloned_path = ws_dir / "calc"
        assert (cloned_path / ".git").exists()
        assert (cloned_path / "calculator.py").exists()

        # Release
        mgr.release("env0")
        assert not lock.lock_path.exists()
