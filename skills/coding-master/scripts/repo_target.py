#!/usr/bin/env python3
"""Two-layer model: WorkspaceContext (lifecycle) + RepoTarget (execution).

Any git / test / engine action receives a RepoTarget, never a raw workspace path.
All resolution logic lives in resolve_repo_target() — commands never do local inference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config_manager import ConfigManager
from workspace import WorkspaceManager, LockFile, ARTIFACT_DIR
from test_runner import _resolve_pytest_command


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data structures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class RepoTarget:
    """Concrete execution target — the only thing git/test/engine see."""
    repo_name: str
    repo_path: str
    test_command: str | None
    git_root: str  # directory containing .git (usually == repo_path)


@dataclass
class WorkspaceContext:
    """Lifecycle container: lock, snapshot, task state."""
    name: str
    path: str
    snapshot: dict | None = None


@dataclass
class RepoTargetBinding:
    """Resolved binding returned by resolve_repo_target()."""
    workspace: WorkspaceContext
    target: RepoTarget


@dataclass
class TestStatus:
    """Explicit test outcome — never silently 'passed' when no command exists."""
    status: str   # "passed" | "failed" | "skipped"
    reason: str   # "success" | "command_missing" | "command_failed"
    report: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unified resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def resolve_repo_target(
    *,
    config: ConfigManager,
    repos_arg: str | None = None,
    workspace_arg: str | None = None,
    repo_arg: str | None = None,
) -> RepoTargetBinding | dict:
    """Single entry point for resolving the execution target.

    Accepts the raw CLI args and returns either:
    - RepoTargetBinding on success
    - error dict {"ok": False, ...} on failure

    Resolution priority:
    1. --workspace + --repo  → explicit workspace + explicit repo
    2. --workspace (no repo) → workspace + auto-detect single repo
    3. --repos               → find/acquire workspace, resolve single repo
    """
    if workspace_arg:
        return _resolve_from_workspace(config, workspace_arg, repo_arg)

    if repos_arg:
        return _resolve_from_repos_arg(config, repos_arg)

    return {
        "ok": False,
        "error": "--repos or --workspace is required",
        "error_code": "INVALID_ARGS",
    }


def resolve_repo_target_for_feature(
    *,
    config: ConfigManager,
    workspace_arg: str,
    repo_arg: str | None = None,
    feature: dict,
) -> RepoTargetBinding | dict:
    """Resolve target for feature mode — workspace must already exist."""
    return _resolve_from_workspace(config, workspace_arg, repo_arg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Workspace → repo lookup helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def find_active_workspaces_by_repos(
    config: ConfigManager,
    repo_names: list[str],
) -> list[dict]:
    """Find active (locked, non-expired) workspaces whose snapshot contains ALL given repos.

    Returns list of {name, path, task, ...} dicts.
    Rules:
    - 0 matches → caller returns NO_WORKSPACE
    - 1 match  → caller proceeds
    - N matches → caller returns AMBIGUOUS_WORKSPACE
    """
    all_ws = config._section().get("workspaces", {})
    matches = []

    for ws_name in all_ws:
        ws = config.get_workspace(ws_name)
        if ws is None:
            continue

        ws_path = ws["path"]
        lock = LockFile(ws_path)
        if not lock.exists():
            continue
        try:
            lock.load()
        except Exception:
            continue
        if lock.is_expired():
            continue

        # Check if this workspace contains the requested repos
        snapshot = _load_workspace_snapshot(ws_path)
        if snapshot is None:
            continue

        snapshot_repo_names = {r.get("name") for r in snapshot.get("repos", [])}
        if all(rn in snapshot_repo_names for rn in repo_names):
            matches.append({
                "name": ws_name,
                "path": ws_path,
                "task": lock.data.get("task", ""),
                "phase": lock.data.get("phase", ""),
            })

    return matches


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test command resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def resolve_test_command(
    repo_path: str,
    repo_name: str,
    config: ConfigManager,
    ws_name: str,
) -> str | None:
    """Determine the test command for a repo target.

    Priority:
    1. Explicit config on workspace.repos.<name>.test_command
    2. Global repo config on repos.<name>.test_command
    3. Auto-detect from pyproject.toml
    """
    ws = config.get_workspace(ws_name)
    if ws and isinstance(ws, dict):
        repos_cfg = ws.get("repos", {})
        if isinstance(repos_cfg, dict):
            repo_cfg = repos_cfg.get(repo_name, {})
            if isinstance(repo_cfg, dict) and repo_cfg.get("test_command"):
                return repo_cfg["test_command"]

    repo_cfg = config.get_repo(repo_name)
    if isinstance(repo_cfg, dict) and repo_cfg.get("test_command"):
        return repo_cfg["test_command"]

    p = Path(repo_path)
    if (p / "pyproject.toml").exists():
        return _resolve_pytest_command(p)
    return None


def run_final_test(target: RepoTarget, config: ConfigManager) -> TestStatus:
    """Run final verification on a RepoTarget. Returns explicit TestStatus."""
    if not target.test_command:
        return TestStatus(status="skipped", reason="command_missing")

    from test_runner import TestRunner as TR, _exec, _parse_pytest_output

    stdout, stderr, rc = _exec(target.repo_path, target.test_command)
    total, passed_count, failed_count = _parse_pytest_output(stdout + stderr)

    from test_runner import _truncate, OUTPUT_MAX
    output = _truncate(stdout + stderr, OUTPUT_MAX)

    if rc == 0:
        return TestStatus(
            status="passed",
            reason="success",
            report={
                "passed_count": passed_count,
                "failed_count": failed_count,
                "total": total,
                "output": output,
            },
        )
    return TestStatus(
        status="failed",
        reason="command_failed",
        report={
            "passed_count": passed_count,
            "failed_count": failed_count,
            "total": total,
            "output": output,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resolve_from_workspace(
    config: ConfigManager,
    ws_name: str,
    repo_arg: str | None,
) -> RepoTargetBinding | dict:
    """Resolve from an explicit workspace name."""
    ws = config.get_workspace(ws_name)
    if ws is None:
        return {
            "ok": False,
            "error": f"workspace '{ws_name}' not found",
            "error_code": "PATH_NOT_FOUND",
        }

    ws_path = ws["path"]

    # Ensure session exists
    session_path = Path(ws_path) / ARTIFACT_DIR / "session.json"
    if not session_path.exists():
        return {
            "ok": False,
            "error": "run workspace-check first to start a session",
            "error_code": "NO_SESSION",
        }

    snapshot = _load_workspace_snapshot(ws_path)
    ws_ctx = WorkspaceContext(name=ws_name, path=ws_path, snapshot=snapshot)

    # Resolve repo within workspace
    target_result = _resolve_single_repo(ws_path, ws_name, snapshot, repo_arg)
    if isinstance(target_result, dict):
        return target_result  # error

    repo_name, repo_path = target_result

    test_cmd = resolve_test_command(repo_path, repo_name, config, ws_name)

    return RepoTargetBinding(
        workspace=ws_ctx,
        target=RepoTarget(
            repo_name=repo_name,
            repo_path=repo_path,
            test_command=test_cmd,
            git_root=repo_path,
        ),
    )


def _resolve_from_repos_arg(
    config: ConfigManager,
    repos_arg: str,
) -> RepoTargetBinding | dict:
    """Resolve from --repos arg: acquire workspace, then resolve single repo."""
    repo_names = [r.strip() for r in repos_arg.split(",") if r.strip()]
    if not repo_names:
        return {
            "ok": False,
            "error": "--repos value is empty",
            "error_code": "INVALID_ARGS",
        }
    if len(repo_names) != 1:
        return {
            "ok": False,
            "error": "auto-dev/submit only supports a single repo target",
            "error_code": "TASK_TOO_COMPLEX",
            "hint": "先运行 analyze 拆分任务，或改为单 repo 调用",
        }

    repo_name = repo_names[0]

    # First check if there's already an active workspace for this repo
    active = find_active_workspaces_by_repos(config, repo_names)
    if len(active) == 1:
        ws_name = active[0]["name"]
        ws_path = active[0]["path"]
    elif len(active) > 1:
        return {
            "ok": False,
            "error": f"multiple active workspaces found for repo '{repo_name}'",
            "error_code": "AMBIGUOUS_WORKSPACE",
            "hint": "请显式传 --workspace，避免操作到错误分支",
        }
    else:
        ws_name = None
        ws_path = None

    if ws_path:
        snapshot = _load_workspace_snapshot(ws_path)
        ws_ctx = WorkspaceContext(name=ws_name, path=ws_path, snapshot=snapshot)

        # Find repo path within workspace
        repo_path = _find_repo_path_in_snapshot(snapshot, repo_name)
        if not repo_path:
            repo_path = str(Path(ws_path) / repo_name)
            if not Path(repo_path).is_dir():
                repo_path = ws_path

        test_cmd = resolve_test_command(repo_path, repo_name, config, ws_name)

        return RepoTargetBinding(
            workspace=ws_ctx,
            target=RepoTarget(
                repo_name=repo_name,
                repo_path=repo_path,
                test_command=test_cmd,
                git_root=repo_path,
            ),
        )

    # No active workspace — return partial binding (workspace needs acquisition)
    # The caller (auto-dev) will need to acquire workspace first
    return {
        "ok": False,
        "error_code": "NEEDS_WORKSPACE_ACQUISITION",
        "data": {"repo_names": repo_names, "repo_name": repo_name},
    }


def _resolve_single_repo(
    ws_path: str,
    ws_name: str,
    snapshot: dict | None,
    repo_arg: str | None,
) -> tuple[str, str] | dict:
    """Resolve a single repo within a workspace. Returns (repo_name, repo_path) or error dict."""
    if not snapshot:
        # No snapshot — workspace is the repo itself
        return (Path(ws_path).name, ws_path)

    repos = snapshot.get("repos", [])

    if not repos:
        # Single-repo workspace (direct workspace mode)
        return (Path(ws_path).name, ws_path)

    if len(repos) == 1:
        only = repos[0]
        return (only["name"], only["path"])

    # Multi-repo workspace — must have explicit --repo
    if repo_arg:
        for repo in repos:
            if repo["name"] == repo_arg:
                return (repo["name"], repo["path"])
        return {
            "ok": False,
            "error": f"repo '{repo_arg}' not found in workspace",
            "error_code": "PATH_NOT_FOUND",
        }

    return {
        "ok": False,
        "error": "multi-repo workspace requires explicit --repo",
        "error_code": "NEED_EXPLICIT_REPO",
        "hint": f"示例: --workspace {ws_name} --repo <name>",
    }


def _load_workspace_snapshot(ws_path: str) -> dict | None:
    """Load workspace_snapshot.json, return parsed dict or None."""
    snap_path = Path(ws_path) / ARTIFACT_DIR / "workspace_snapshot.json"
    if not snap_path.is_file():
        return None
    try:
        return json.loads(snap_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _find_repo_path_in_snapshot(snapshot: dict | None, repo_name: str) -> str | None:
    """Find a repo's path within a workspace snapshot."""
    if not snapshot:
        return None
    for repo in snapshot.get("repos", []):
        if repo.get("name") == repo_name:
            return repo.get("path")
    return None
