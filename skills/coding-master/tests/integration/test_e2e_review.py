"""End-to-end test for the deep review workflow.

Exercises the review pipeline:
  quick-status --repos → workspace-check --repos → analyze → release

Only mocks the engine (no real LLM call). All other components are real:
real git repo, real workspace management, real lock files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ── path setup ──────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager
from workspace import WorkspaceManager, LockFile, ARTIFACT_DIR
from test_runner import TestRunner
from engine import EngineResult
from engine.claude_runner import ClaudeRunner
from engine.codex_runner import CodexRunner

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Project fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SAMPLE_PY = textwrap.dedent("""\
    \"\"\"Sample module with a few issues for review.\"\"\"

    import os
    import sys  # unused import


    def fetch_data(url):
        import urllib.request
        return urllib.request.urlopen(url).read()  # no timeout, no error handling


    def process(items):
        result = []
        for i in items:
            result.append(i * 2)
        return result
""")

TEST_SAMPLE_PY = textwrap.dedent("""\
    from sample import process


    def test_process():
        assert process([1, 2, 3]) == [2, 4, 6]
""")

PYPROJECT_TOML = textwrap.dedent("""\
    [project]
    name = "sample"
    version = "0.1.0"

    [tool.pytest.ini_options]
    testpaths = ["."]
""")

REVIEW_SUMMARY = textwrap.dedent("""\
    ## Review Summary

    ### High Priority
    - H1: `fetch_data` has no timeout and no error handling — can hang or crash
    - H2: No input validation on URL parameter — potential SSRF

    ### Medium Priority
    - M1: `process` could use list comprehension for clarity
    - M2: Unused import `sys` in sample.py

    ### Low Priority
    - L1: Missing type hints on public functions
    - L2: No docstrings on `process` function

    **Complexity**: standard
""")

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


@pytest.fixture
def review_project(tmp_path):
    """Create a real git repo with a sample project for review."""
    # Source repo (local path, registered as repo for quick-status --repos)
    repo = tmp_path / "sample_project"
    repo.mkdir()

    (repo / "sample.py").write_text(SAMPLE_PY)
    (repo / "test_sample.py").write_text(TEST_SAMPLE_PY)
    (repo / "pyproject.toml").write_text(PYPROJECT_TOML)

    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")

    # Bare remote for clone-based workspace-check
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--bare")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")

    # Workspace slot
    ws_dir = tmp_path / "workspaces" / "env0"
    ws_dir.mkdir(parents=True)

    # Config: repo url points to local working copy (for quick-status --repos
    # and _resolve_repo_paths). workspace-check --repos uses the same URL
    # to clone from.
    cfg_path = tmp_path / "config.yaml"
    data = {
        "coding_master": {
            "repos": {"sample": str(repo)},
            "workspaces": {"env0": str(ws_dir)},
            "envs": {},
            "default_engine": "codex",
            "max_turns": 5,
        }
    }
    cfg_path.write_text(yaml.dump(data))

    return {
        "repo": repo,
        "origin": origin,
        "ws_dir": ws_dir,
        "cfg_path": cfg_path,
        "config": ConfigManager(config_path=cfg_path),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeepReviewWorkflow:
    """Full deep review pipeline: quick-status → workspace-check → analyze → release."""

    TASK = "review: review sample project for issues and improvements"

    # ── Step 1: quick-status --repos (lock-free) ──────

    def test_step1_quick_status_repos(self, review_project):
        """quick-status --repos returns git/runtime/project info without lock."""
        config = review_project["config"]
        repo = review_project["repo"]

        mgr = WorkspaceManager(config)

        # Simulate dispatch args
        import argparse
        args = argparse.Namespace(repos="sample", workspace=None)

        # Import and call the dispatch handler directly
        from dispatch import cmd_quick_status
        with patch("dispatch.ConfigManager", return_value=config):
            result = cmd_quick_status(args)

        assert result["ok"] is True
        repos_data = result["data"]["repos"]
        assert "sample" in repos_data

        sample = repos_data["sample"]
        assert sample["git"]["branch"] == "main"
        # Runtime and project detection depends on repo having source files
        assert "runtime" in sample
        assert "project" in sample

    # ── Step 2: workspace-check --repos ──────────────

    def test_step2_workspace_check_repos(self, review_project):
        """workspace-check --repos acquires lock and creates snapshot."""
        config = review_project["config"]
        ws_dir = review_project["ws_dir"]

        mgr = WorkspaceManager(config)
        result = mgr.check_and_acquire_for_repos(
            ["sample"], self.TASK, "codex"
        )

        assert result["ok"] is True
        snapshot = result["data"]["snapshot"]

        # Workspace assigned
        assert snapshot["workspace"]["name"] == "env0"

        # Repo cloned and probed
        assert len(snapshot["repos"]) == 1
        repo_info = snapshot["repos"][0]
        assert repo_info["name"] == "sample"
        assert repo_info["git"]["branch"] is not None
        assert repo_info["runtime"]["type"] == "python"

        # Lock created
        lock = LockFile(str(ws_dir))
        assert lock.exists()
        lock.load()
        assert lock.data["task"] == self.TASK
        assert lock.data["engine"] == "codex"

    # ── Step 3: analyze with engine (mock) ───────────

    def test_step3_analyze_returns_review(self, review_project):
        """analyze produces structured review with complexity assessment."""
        config = review_project["config"]
        ws_dir = review_project["ws_dir"]

        # Setup: workspace-check first
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire_for_repos(["sample"], self.TASK, "codex")

        fake_result = EngineResult(
            success=True,
            summary=REVIEW_SUMMARY,
            files_changed=[],
        )

        with patch.object(CodexRunner, "run", return_value=fake_result):
            import argparse
            args = argparse.Namespace(
                workspace="env0",
                task="Full project review: identify high-priority issues",
                engine="codex",
                _ws_path=str(ws_dir),
            )
            from dispatch import cmd_analyze
            with patch("dispatch.ConfigManager", return_value=config):
                result = cmd_analyze(args)

        assert result["ok"] is True
        data = result["data"]

        # Review summary returned
        assert "High Priority" in data["summary"]
        assert "fetch_data" in data["summary"]
        assert data["complexity"] == "standard"
        assert data["feature_plan_created"] is False

        # Analysis artifact saved
        analysis_path = ws_dir / ARTIFACT_DIR / "phase2_analysis.md"
        assert analysis_path.exists()
        content = analysis_path.read_text()
        assert "High Priority" in content

        # Lock updated to analyzing phase
        lock = LockFile(str(ws_dir))
        lock.load()
        assert lock.data["phase"] == "analyzing"

    # ── Step 4: release ──────────────────────────────

    def test_step4_release(self, review_project):
        """release frees the workspace lock."""
        config = review_project["config"]
        ws_dir = review_project["ws_dir"]

        # Setup: workspace-check
        mgr = WorkspaceManager(config)
        mgr.check_and_acquire_for_repos(["sample"], self.TASK, "codex")

        lock = LockFile(str(ws_dir))
        assert lock.exists()

        # Release
        result = mgr.release("env0")
        assert result["ok"] is True
        assert not lock.lock_path.exists()

    # ── Full pipeline ────────────────────────────────

    def test_full_review_pipeline(self, review_project):
        """Full pipeline: quick-status → workspace-check → analyze → release."""
        config = review_project["config"]
        ws_dir = review_project["ws_dir"]

        # Step 1: quick-status --repos (lock-free)
        import argparse
        qs_args = argparse.Namespace(repos="sample", workspace=None)
        from dispatch import cmd_quick_status
        with patch("dispatch.ConfigManager", return_value=config):
            r1 = cmd_quick_status(qs_args)
        assert r1["ok"] is True
        assert "sample" in r1["data"]["repos"]

        # Step 2: workspace-check --repos
        mgr = WorkspaceManager(config)
        r2 = mgr.check_and_acquire_for_repos(
            ["sample"], self.TASK, "codex"
        )
        assert r2["ok"] is True
        ws_name = "env0"

        # Step 3: analyze (mock engine)
        fake_result = EngineResult(
            success=True,
            summary=REVIEW_SUMMARY,
            files_changed=[],
        )
        with patch.object(CodexRunner, "run", return_value=fake_result):
            analyze_args = argparse.Namespace(
                workspace=ws_name,
                task="Full project review",
                engine="codex",
                _ws_path=str(ws_dir),
            )
            from dispatch import cmd_analyze
            with patch("dispatch.ConfigManager", return_value=config):
                r3 = cmd_analyze(analyze_args)
        assert r3["ok"] is True
        assert "High Priority" in r3["data"]["summary"]
        assert r3["data"]["complexity"] == "standard"

        # Verify artifacts exist
        assert (ws_dir / ARTIFACT_DIR / "workspace_snapshot.json").exists()
        assert (ws_dir / ARTIFACT_DIR / "phase2_analysis.md").exists()

        # Lock state before release
        lock = LockFile(str(ws_dir))
        lock.load()
        assert lock.data["phase"] == "analyzing"
        assert lock.data["task"] == self.TASK
        phases = [h["phase"] for h in lock.data["phase_history"]]
        assert "workspace-check" in phases

        # Step 4: release
        r4 = mgr.release(ws_name)
        assert r4["ok"] is True
        assert not lock.lock_path.exists()

    # ── Engine fallback ──────────────────────────────

    def test_analyze_engine_fallback(self, review_project):
        """If codex fails, retry with claude (engine fallback)."""
        config = review_project["config"]
        ws_dir = review_project["ws_dir"]

        mgr = WorkspaceManager(config)
        mgr.check_and_acquire_for_repos(["sample"], self.TASK, "codex")

        codex_fail = EngineResult(success=False, summary="", files_changed=[], error="timeout")
        claude_ok = EngineResult(success=True, summary=REVIEW_SUMMARY, files_changed=[])

        import argparse

        # First attempt with codex → fail
        with patch.object(CodexRunner, "run", return_value=codex_fail):
            args1 = argparse.Namespace(
                workspace="env0", task="review", engine="codex",
                _ws_path=str(ws_dir),
            )
            from dispatch import cmd_analyze
            with patch("dispatch.ConfigManager", return_value=config):
                r1 = cmd_analyze(args1)
        assert r1["ok"] is False
        assert r1["error_code"] == "ENGINE_ERROR"

        # Retry with claude → success
        with patch.object(ClaudeRunner, "run", return_value=claude_ok):
            args2 = argparse.Namespace(
                workspace="env0", task="review", engine="claude",
                _ws_path=str(ws_dir),
            )
            with patch("dispatch.ConfigManager", return_value=config):
                r2 = cmd_analyze(args2)
        assert r2["ok"] is True
        assert "High Priority" in r2["data"]["summary"]

    # ── Negative: release without analyze ────────────

    def test_release_without_analyze_works(self, review_project):
        """User can release workspace even if they skip analysis."""
        config = review_project["config"]
        ws_dir = review_project["ws_dir"]

        mgr = WorkspaceManager(config)
        mgr.check_and_acquire_for_repos(["sample"], self.TASK, "codex")

        # Release immediately (user cancels)
        result = mgr.release("env0")
        assert result["ok"] is True
        assert not LockFile(str(ws_dir)).lock_path.exists()
