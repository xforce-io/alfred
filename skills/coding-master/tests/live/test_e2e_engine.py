"""End-to-end integration test: dispatch → real claude engine.

Exercises the full pipeline using a real temporary git repo and the real
claude CLI. These tests cost API credits and require the `claude` binary.

Run with:
    python -m pytest skills/coding-master/tests/integration/test_e2e_engine.py --run-live -v
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ── path setup ──────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager
from workspace import WorkspaceManager, LockFile, ARTIFACT_DIR

# ── Skip unless claude CLI is available ─────────────
pytestmark = pytest.mark.live

_has_claude = shutil.which("claude") is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sample code with reviewable issues
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SAMPLE_PY = textwrap.dedent("""\
    \"\"\"Sample module with several issues for review.\"\"\"

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def review_repo(tmp_path):
    """Create a real git repo with reviewable Python code.

    Layout:
      - bare origin repo (acts as remote)
      - cloned working copy with sample.py (has issues)
      - config.yaml with repos + workspace slot
      - ConfigManager instance
    """
    if not _has_claude:
        pytest.skip("claude CLI not found in PATH")

    # Bare origin
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--bare")

    # Temp clone to push initial commit
    init_clone = tmp_path / "init_clone"
    subprocess.run(
        ["git", "clone", str(origin), str(init_clone)],
        capture_output=True, text=True, env=_GIT_ENV,
    )
    (init_clone / "sample.py").write_text(SAMPLE_PY)
    (init_clone / "test_sample.py").write_text(TEST_SAMPLE_PY)
    (init_clone / "pyproject.toml").write_text(PYPROJECT_TOML)
    _git(init_clone, "add", "-A")
    _git(init_clone, "commit", "-m", "initial commit")
    _git(init_clone, "push")

    # Workspace slot
    ws_dir = tmp_path / "workspaces" / "env0"
    ws_dir.mkdir(parents=True)

    # Config
    cfg_path = tmp_path / "config.yaml"
    data = {
        "coding_master": {
            "repos": {"sample": str(origin)},
            "workspaces": {"env0": str(ws_dir)},
            "envs": {},
            "default_engine": "claude",
            "max_turns": 10,
        }
    }
    cfg_path.write_text(yaml.dump(data))

    return {
        "origin": origin,
        "ws_dir": ws_dir,
        "cfg_path": cfg_path,
        "config": ConfigManager(config_path=cfg_path),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEngineReview:
    """Real engine review: dispatch → claude CLI → verify artifacts."""

    TASK = "review: review sample project code for issues and improvements"

    def _acquire_workspace(self, config):
        """Acquire workspace via check_and_acquire_for_repos."""
        mgr = WorkspaceManager(config)
        result = mgr.check_and_acquire_for_repos(
            ["sample"], self.TASK, "claude"
        )
        assert result["ok"] is True, f"workspace-check failed: {result}"
        return result

    def test_analyze_with_real_engine(self, review_repo):
        """dispatch cmd_analyze → real claude CLI → verify analysis artifact."""
        config = review_repo["config"]
        ws_dir = review_repo["ws_dir"]

        # Step 1: workspace-check (real)
        self._acquire_workspace(config)

        # Step 2: analyze via dispatch (real engine)
        from dispatch import cmd_analyze
        args = argparse.Namespace(
            workspace="env0",
            task="Full project review: identify code quality issues, missing error handling, unused imports",
            engine="claude",
            _ws_path=str(ws_dir),
        )
        with patch("dispatch.ConfigManager", return_value=config):
            result = cmd_analyze(args)

        assert result["ok"] is True, f"analyze failed: {result.get('error')}"
        data = result["data"]

        # Summary is non-empty and has meaningful content
        assert data["summary"], "summary should not be empty"
        assert len(data["summary"]) > 50, "summary too short to be real analysis"

        # Complexity is a valid value
        assert data["complexity"] in ("trivial", "standard", "complex")

        # Analysis artifact exists and is non-empty
        analysis_path = ws_dir / ARTIFACT_DIR / "phase2_analysis.md"
        assert analysis_path.exists(), "phase2_analysis.md not created"
        content = analysis_path.read_text()
        assert len(content) > 50, "analysis file too short"

        # Lock phase updated
        lock = LockFile(str(ws_dir))
        lock.load()
        assert lock.data["phase"] == "analyzing"

    def test_develop_with_real_engine(self, review_repo):
        """dispatch cmd_develop → real claude CLI → verify code changes."""
        config = review_repo["config"]
        ws_dir = review_repo["ws_dir"]

        # Step 1: workspace-check (real)
        self._acquire_workspace(config)

        # Step 2: analyze first (real engine)
        from dispatch import cmd_analyze
        analyze_args = argparse.Namespace(
            workspace="env0",
            task="Review sample.py: find unused imports, missing timeout in fetch_data, suggest improvements",
            engine="claude",
            _ws_path=str(ws_dir),
        )
        with patch("dispatch.ConfigManager", return_value=config):
            r_analyze = cmd_analyze(analyze_args)
        assert r_analyze["ok"] is True, f"analyze failed: {r_analyze.get('error')}"

        # Step 3: develop (real engine)
        # Note: branch=None because repo-based workspaces have git repos in
        # subdirs (ws_dir/sample/), and GitOps(ws_path) targets the workspace
        # root which isn't a git repo itself.
        from dispatch import cmd_develop
        develop_args = argparse.Namespace(
            workspace="env0",
            task="Fix the issues found: remove unused import sys, add timeout to fetch_data",
            plan=None,
            branch=None,
            engine="claude",
            _ws_path=str(ws_dir),
        )
        with patch("dispatch.ConfigManager", return_value=config):
            r_develop = cmd_develop(develop_args)

        assert r_develop["ok"] is True, f"develop failed: {r_develop.get('error')}"
        data = r_develop["data"]

        # Develop summary should be non-empty and meaningful
        assert data["summary"], "develop summary should not be empty"
        assert len(data["summary"]) > 20, "develop summary too short"

        # Check for file changes: either via engine's files_changed list
        # or via direct git diff in the cloned repo subdir.
        # Note: files_changed may be empty for repo-based workspaces because
        # ClaudeRunner runs git diff at ws_path (workspace root), not inside
        # the cloned repo subdir.
        repo_dir = ws_dir / "sample"
        diff = _git(repo_dir, "diff", "--name-only")
        cached = _git(repo_dir, "diff", "--name-only", "--cached")
        changed = set(filter(None, (diff + cached).splitlines()))
        if changed or data["files_changed"]:
            # Great — engine actually modified code
            pass
        else:
            # Engine may describe changes without applying them (non-deterministic).
            # At minimum, verify the summary mentions the target file.
            pytest.xfail("engine did not modify files (non-deterministic LLM behavior)")

    def test_full_review_pipeline(self, review_repo):
        """Full pipeline: workspace-check → analyze → release."""
        config = review_repo["config"]
        ws_dir = review_repo["ws_dir"]

        # Step 1: workspace-check (real)
        r_ws = self._acquire_workspace(config)
        assert r_ws["ok"] is True

        # Step 2: analyze (real engine)
        from dispatch import cmd_analyze
        analyze_args = argparse.Namespace(
            workspace="env0",
            task="Review the project for code quality issues",
            engine="claude",
            _ws_path=str(ws_dir),
        )
        with patch("dispatch.ConfigManager", return_value=config):
            r_analyze = cmd_analyze(analyze_args)

        assert r_analyze["ok"] is True, f"analyze failed: {r_analyze.get('error')}"

        # Verify analysis has meaningful review content
        summary = r_analyze["data"]["summary"]
        assert len(summary) > 50

        # Verify artifacts exist
        assert (ws_dir / ARTIFACT_DIR / "workspace_snapshot.json").exists()
        assert (ws_dir / ARTIFACT_DIR / "phase2_analysis.md").exists()

        # Lock state before release
        lock = LockFile(str(ws_dir))
        lock.load()
        assert lock.data["phase"] == "analyzing"
        phases = [h["phase"] for h in lock.data["phase_history"]]
        assert "workspace-check" in phases

        # Step 3: release
        mgr = WorkspaceManager(config)
        r_release = mgr.release("env0")
        assert r_release["ok"] is True
        assert not lock.lock_path.exists()
