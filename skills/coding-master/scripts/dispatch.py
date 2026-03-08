#!/usr/bin/env python3
"""CLI router for coding-master skill."""

from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
from pathlib import Path

# Ensure scripts/ is on sys.path for sibling imports
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager
from workspace import WorkspaceManager, LockFile, ARTIFACT_DIR
from env_probe import EnvProber
from feature_manager import FeatureManager
from test_runner import TestRunner
from git_ops import GitOps
from engine.claude_runner import ClaudeRunner
from engine.codex_runner import CodexRunner


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prompt templates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ANALYZE_PROMPT = """\
## Development Environment (Workspace)
{workspace_snapshot}

## Runtime Environment (Env)
{env_snapshot}

## Task
Analyze the following issue. Do NOT modify any code.
Issue: {task}

Output:
1. Problem location: which files and functions are involved
2. Root cause analysis: correlate with runtime logs if available
3. Fix proposals (multiple if applicable, mark recommended)
4. Impact scope
5. Risk assessment (low / medium / high)
6. Whether more Env information is needed
7. Complexity: classify as exactly one of `trivial`, `standard`, or `complex`
   - trivial: single-file typo/config fix, <10 lines changed
   - standard: focused bug fix or small feature, 1-3 files
   - complex: multi-file refactor, new subsystem, or cross-cutting concern requiring task splitting
8. If Complexity is `complex`, output a Feature Plan as a JSON code block:
   ```json
   [
     {{"title": "...", "task": "...", "depends_on": [], "acceptance_criteria": [
       {{"type": "test", "target": "pytest tests/...", "description": "..."}},
       {{"type": "assert", "description": "..."}},
       {{"type": "manual", "description": "..."}}
     ]}}
   ]
   ```
"""

DEVELOP_PROMPT = """\
## Development Environment (Workspace)
{workspace_snapshot}

## Diagnosis Report
{analysis}

## User-Confirmed Plan
{plan}

## Task
Implement the fix/feature based on the diagnosis report above.
Task: {task}

## Test Command
{test_command}

Rules:
- Only modify files within this repository
- After implementing, run the test command to verify
- If tests fail, fix the code and re-run until tests pass
- Do NOT commit — that will be done separately
- Keep changes minimal and focused
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Lock-aware wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_workspace_path(args) -> str | None:
    """Get workspace path from args.workspace name.

    When ``args.workspace`` is None, auto-detect the sole active (locked,
    non-expired) workspace so callers can omit ``--workspace``.
    """
    ws_name = getattr(args, "workspace", None)

    if ws_name is None:
        # Auto-detect: if exactly one workspace has an active lock, use it.
        sole = _find_sole_active_workspace()
        if sole is None:
            return None
        ws_name = sole
        args.workspace = sole       # back-fill so downstream code sees it

    config = ConfigManager()
    ws = config.get_workspace(ws_name)
    if ws is None:
        return None
    return ws["path"]


def _find_sole_active_workspace() -> str | None:
    """Return the workspace name if exactly one non-expired lock exists, else None."""
    active = _collect_workspace_status()
    if len(active) == 1:
        return active[0]["name"]
    return None


def _resolve_repo_paths(args, config: ConfigManager | None = None) -> dict | list[tuple[str, str]]:
    """Resolve --repos to list of (name, path) tuples.

    Returns an error dict on failure, or a list of (name, path) on success.
    """
    config = config or ConfigManager()
    repo_names = [r.strip() for r in args.repos.split(",") if r.strip()]
    if not repo_names:
        avail = _available_names(config)
        return {
            "ok": False,
            "error": "--repos value is empty. Provide at least one repo name.",
            "error_code": "INVALID_ARGS",
            "hint": f"Available repos: {avail['repos']}.",
        }
    result = []
    for name in repo_names:
        repo = config.get_repo(name)
        if repo is None:
            avail = _available_names(config)
            return {
                "ok": False,
                "error": f"repo '{name}' not found. Use config-list to see registered repos.",
                "error_code": "PATH_NOT_FOUND",
                "hint": f"Available repos: {avail['repos']}.",
            }
        repo_url = repo.get("url", "")
        repo_path = Path(repo_url).expanduser()
        if not repo_path.is_dir():
            return {
                "ok": False,
                "error": f"repo '{name}' path '{repo_url}' is not a local directory.",
                "error_code": "PATH_NOT_FOUND",
            }
        result.append((name, str(repo_path)))
    return result


def with_lock_update(workspace_path: str, phase: str, fn, *args, **kwargs) -> dict:
    """Verify lock → run fn → update phase → renew lease → save."""
    lock = LockFile(workspace_path)
    try:
        lock.verify_active()
    except RuntimeError as e:
        error_code = "LEASE_EXPIRED" if "expired" in str(e) else "LOCK_NOT_FOUND"
        return {"ok": False, "error": str(e), "error_code": error_code}

    result = fn(*args, **kwargs)

    # Update lock on success
    if isinstance(result, dict) and result.get("ok", True):
        lock.update_phase(phase)
        lock.renew_lease()
        lock.save()

    _sync_coding_stats()
    return result


def requires_workspace(fn):
    """Decorator: enforce workspace-check was called, inject ws_path from session.

    When ``--workspace`` is omitted, auto-detection kicks in via
    ``_resolve_workspace_path`` (picks the sole active workspace).
    """
    @functools.wraps(fn)
    def wrapper(args):
        ws_path = _resolve_workspace_path(args)
        if ws_path is None:
            avail = _available_names(ConfigManager())
            active = _collect_workspace_status()
            if not getattr(args, "workspace", None) and len(active) != 1:
                hint = (
                    f"No --workspace specified and {len(active)} active workspace(s) found. "
                    f"Pass --workspace explicitly."
                )
            else:
                hint = f"Available workspaces: {avail['workspaces']}."
            return {"ok": False,
                    "error": f"workspace '{getattr(args, 'workspace', None)}' not found",
                    "error_code": "PATH_NOT_FOUND",
                    "hint": hint}
        session_path = Path(ws_path) / ARTIFACT_DIR / "session.json"
        if not session_path.exists():
            return {"ok": False,
                    "error": "run workspace-check first to start a session",
                    "error_code": "NO_SESSION"}
        session = json.loads(session_path.read_text())
        args._ws_path = session["ws_path"]
        return fn(args)
    return wrapper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Engine helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _get_engine(name: str):
    if name == "claude":
        return ClaudeRunner()
    if name == "codex":
        return CodexRunner()
    return None


import re as _re


def _parse_complexity(summary: str) -> str:
    """Extract complexity classification from engine summary."""
    m = _re.search(r"Complexity:\s*(trivial|standard|complex)", summary, _re.IGNORECASE)
    return m.group(1).lower() if m else "standard"


def _parse_feature_plan(summary: str) -> list[dict] | None:
    """Extract feature plan JSON from ```json code block in summary."""
    m = _re.search(r"```json\s*\n(\[[\s\S]*?\])\s*\n```", summary)
    if not m:
        return None
    try:
        plan = json.loads(m.group(1))
        if isinstance(plan, list) and len(plan) > 0:
            return plan
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _load_artifact(ws_path: str, filename: str) -> str:
    p = Path(ws_path) / ARTIFACT_DIR / filename
    if p.exists():
        return p.read_text()
    return "(not available)"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  auto-dev helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _looks_complex(task: str) -> bool:
    """Heuristic: does the task description suggest multi-feature complexity?"""
    complex_signals = ["重构", "refactor", "redesign", "整个", "所有", "全部",
                       "多模块", "cross-cutting", "新子系统", "new subsystem"]
    task_lower = task.lower()
    return sum(1 for s in complex_signals if s in task_lower) >= 2




def _clean_workspace_repos(ws_path: str, workspace_snapshot: str) -> dict:
    """Reset tracked/untracked changes for repos inside the workspace snapshot."""
    try:
        snapshot = json.loads(workspace_snapshot)
    except Exception:
        return {
            "ok": False,
            "error": "workspace snapshot unavailable for reset",
            "error_code": "NO_SNAPSHOT",
            "hint": "先运行 workspace-check，或手动清理 workspace 后重试",
        }

    repos = snapshot.get("repos", [])
    if not repos:
        # Single-repo workspace — clean the root
        result = GitOps.force_clean(ws_path)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": result.get("error", f"failed to clean: {ws_path}"),
                "error_code": "GIT_ERROR",
            }
        return {"ok": True}

    for repo in repos:
        repo_path = repo.get("path")
        if not repo_path:
            continue
        result = GitOps.force_clean(repo_path)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": result.get("error", f"failed to clean repo: {repo_path}"),
                "error_code": "GIT_ERROR",
                "hint": f"手动检查仓库后重试: {repo_path}",
            }

    return {"ok": True}


def _suggest_next(args, feature_mode: bool, tests_passed: bool) -> str:
    workspace_mode = bool(getattr(args, "workspace", None)) and not feature_mode

    if feature_mode:
        if tests_passed:
            return (
                f"当前 feature 完成。继续下一个 feature: "
                f"$D auto-dev --workspace {args.workspace} --feature next"
            )
        return (
            f"当前 feature 验证失败。两个选择:\n"
            f"1. 继续修复: $D auto-dev --workspace {args.workspace} --feature next\n"
            f"2. 清空改动后重试: $D auto-dev --workspace {args.workspace} --feature next --reset-worktree"
        )

    if workspace_mode:
        if tests_passed:
            return f"测试通过。提交 PR: $D submit --workspace {args.workspace} --title \"<title>\""
        return (
            f"测试未全部通过。两个选择:\n"
            f"1. 继续修复: $D auto-dev --workspace {args.workspace} --task \"fix failing tests\"\n"
            f"2. 清空改动后重试: $D auto-dev --workspace {args.workspace} --task \"{getattr(args, 'task', '')}\" --reset-worktree"
        )

    repos = getattr(args, "repos", "")
    if tests_passed:
        return f"测试通过。提交 PR: $D submit --repos {repos} --title \"<title>\""

    return (
        f"测试未全部通过。两个选择:\n"
        f"1. 继续修复: $D auto-dev --repos {repos} --task \"fix failing tests\"\n"
        f"2. 清空改动后重试: $D auto-dev --repos {repos} --task \"{getattr(args, 'task', '')}\" --reset-worktree"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Command handlers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_config_list(args) -> dict:
    return ConfigManager().list_all()


def cmd_config_add(args) -> dict:
    return ConfigManager().add(args.kind, args.name, args.value)


def cmd_config_set(args) -> dict:
    return ConfigManager().set_field(args.kind, args.name, args.key, args.value)


def cmd_config_remove(args) -> dict:
    return ConfigManager().remove(args.kind, args.name)


# ── Quick queries (lock-free, read-only) ─────────────────

def cmd_quick_status(args) -> dict:
    """Workspace overview: git info, runtime, project commands, lock status."""
    config = ConfigManager()

    # Validate: at least one of --workspace or --repos must be provided
    if not getattr(args, "workspace", None) and not getattr(args, "repos", None):
        avail = _available_names(config)
        return {
            "ok": False,
            "error": "Either --workspace or --repos is required.",
            "error_code": "INVALID_ARGS",
            "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}.",
        }

    # --repos mode: probe each repo path directly (no lock needed)
    if getattr(args, "repos", None):
        resolved = _resolve_repo_paths(args, config)
        if isinstance(resolved, dict):
            return resolved  # error

        mgr = WorkspaceManager(config)
        repos_data = {}
        for name, repo_path in resolved:
            repo_cfg = config.get_repo(name)
            git_info = mgr._probe_git(repo_path)
            runtime = mgr._probe_runtime(repo_path)
            project = mgr._probe_project(repo_path, repo_cfg)
            repos_data[name] = {
                "path": repo_path,
                "git": git_info,
                "runtime": runtime,
                "project": project,
            }
        return {"ok": True, "data": {"repos": repos_data}}

    # --workspace mode (original behavior)
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        avail = _available_names(config)
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND",
                "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}."}

    mgr = WorkspaceManager(config)
    ws = config.get_workspace(args.workspace)

    git_info = mgr._probe_git(ws_path)
    runtime = mgr._probe_runtime(ws_path)
    project = mgr._probe_project(ws_path, ws)

    # Read-only lock peek
    lock_info = None
    lock = LockFile(ws_path)
    if lock.exists():
        lock.load()
        lock_info = {
            "task": lock.data.get("task"),
            "phase": lock.data.get("phase"),
            "engine": lock.data.get("engine"),
            "started_at": lock.data.get("started_at"),
            "expired": lock.is_expired(),
        }

    return {
        "ok": True,
        "data": {
            "workspace": args.workspace,
            "path": ws_path,
            "git": git_info,
            "runtime": runtime,
            "project": project,
            "lock": lock_info,
        },
    }


def cmd_quick_test(args) -> dict:
    """Run tests (and optionally lint) without acquiring a lock."""
    config = ConfigManager()

    # Validate: at least one of --workspace or --repos must be provided
    if not getattr(args, "workspace", None) and not getattr(args, "repos", None):
        avail = _available_names(config)
        return {
            "ok": False,
            "error": "Either --workspace or --repos is required.",
            "error_code": "INVALID_ARGS",
            "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}.",
        }

    # --repos mode: run tests in each repo path directly
    if getattr(args, "repos", None):
        resolved = _resolve_repo_paths(args, config)
        if isinstance(resolved, dict):
            return resolved  # error

        from dataclasses import asdict
        runner = TestRunner(config)
        repos_data = {}
        overall = "passed"

        for name, repo_path in resolved:
            repo_cfg = config.get_repo(name)
            commands = runner._detect_commands(repo_path, repo_cfg)
            test_cmd = commands.get("test_command")
            if test_cmd and getattr(args, "path", None):
                test_cmd = f"{test_cmd} {args.path}"

            test_result = runner._run_test(repo_path, test_cmd)
            entry = {"test": asdict(test_result), "overall": "passed" if test_result.passed else "failed"}

            if getattr(args, "lint", False):
                lint_result = runner._run_lint(repo_path, commands.get("lint_command"))
                entry["lint"] = asdict(lint_result)
                if not lint_result.passed:
                    entry["overall"] = "failed"

            if entry["overall"] == "failed":
                overall = "failed"
            repos_data[name] = entry

        return {"ok": True, "data": {"repos": repos_data, "overall": overall}}

    # --workspace mode (original behavior)
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        avail = _available_names(config)
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND",
                "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}."}

    runner = TestRunner(config)
    ws = config.get_workspace(args.workspace)
    commands = runner._detect_commands(ws_path, ws)

    test_cmd = commands.get("test_command")
    if test_cmd and args.path:
        test_cmd = f"{test_cmd} {args.path}"

    test_result = runner._run_test(ws_path, test_cmd)

    from dataclasses import asdict
    data = {"test": asdict(test_result), "overall": "passed" if test_result.passed else "failed"}

    if args.lint:
        lint_result = runner._run_lint(ws_path, commands.get("lint_command"))
        data["lint"] = asdict(lint_result)
        if not lint_result.passed:
            data["overall"] = "failed"

    return {"ok": True, "data": data}


_QUICK_FIND_MAX = 100


def _grep_in_path(search_path: str, query: str, glob: str | None) -> dict:
    """Run grep in a directory and return result dict."""
    cmd = ["grep", "-rn", query, "."]
    if glob:
        cmd = ["grep", "-rn", f"--include={glob}", query, "."]

    try:
        r = subprocess.run(cmd, cwd=search_path, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "search timed out (30s)", "error_code": "TIMEOUT"}

    lines = r.stdout.strip().split("\n") if r.stdout.strip() else []
    truncated = len(lines) > _QUICK_FIND_MAX
    lines = lines[:_QUICK_FIND_MAX]
    return {"ok": True, "lines": lines, "truncated": truncated}


def _available_names(config: ConfigManager) -> dict:
    """Return available workspace and repo names for error hints."""
    data = config.list_all().get("data", {})
    return {
        "workspaces": list(data.get("workspaces", {}).keys()),
        "repos": list(data.get("repos", {}).keys()),
    }


def cmd_quick_find(args) -> dict:
    """Search code in workspace or repo source path via grep."""
    config = ConfigManager()

    # Validate: at least one of --workspace or --repos must be provided
    if not args.workspace and not args.repos:
        avail = _available_names(config)
        return {
            "ok": False,
            "error": "Either --workspace or --repos is required.",
            "error_code": "INVALID_ARGS",
            "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}.",
        }

    # --repos mode: search directly in repo source paths (no lock needed)
    if args.repos:
        resolved = _resolve_repo_paths(args, config)
        if isinstance(resolved, dict):
            return resolved  # error
        all_results: dict = {}
        total_count = 0

        for name, repo_path in resolved:
            result = _grep_in_path(repo_path, args.query, args.glob)
            if not result["ok"]:
                return result
            all_results[name] = result["lines"]
            total_count += len(result["lines"])

        return {
            "ok": True,
            "data": {
                "query": args.query,
                "glob": args.glob,
                "repos": all_results,
                "count": total_count,
                "truncated": any(
                    len(lines) >= _QUICK_FIND_MAX for lines in all_results.values()
                ),
            },
        }

    # --workspace mode (original behavior)
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        avail = _available_names(config)
        return {
            "ok": False,
            "error": f"workspace '{args.workspace}' not found.",
            "error_code": "PATH_NOT_FOUND",
            "hint": (
                f"Available workspaces: {avail['workspaces']}. "
                f"Available repos: {avail['repos']}. "
                "Use --repos <name> to search a registered repo directly."
            ),
        }

    result = _grep_in_path(ws_path, args.query, args.glob)
    if not result["ok"]:
        return result

    return {
        "ok": True,
        "data": {
            "query": args.query,
            "glob": args.glob,
            "matches": result["lines"],
            "count": len(result["lines"]),
            "truncated": result["truncated"],
        },
    }


def cmd_quick_env(args) -> dict:
    """Probe env without workspace — pure observation, no artifacts."""
    config = ConfigManager()
    prober = EnvProber(config)
    extra = args.commands if hasattr(args, "commands") and args.commands else None
    return prober.probe(args.env, extra_commands=extra)


def cmd_workspace_check(args) -> dict:
    config = ConfigManager()
    engine = args.engine or config.get_default_engine()
    mgr = WorkspaceManager(config)

    if args.repos:
        # Repo mode: clone/update repos into workspace
        repo_names = [r.strip() for r in args.repos.split(",") if r.strip()]
        result = mgr.check_and_acquire_for_repos(
            repo_names, args.task, engine,
            workspace_name=args.workspace,
            auto_clean=getattr(args, "auto_clean", False),
        )
    elif not args.workspace:
        result = {"ok": False, "error": "--workspace is required when --repos is not provided",
                "error_code": "INVALID_ARGS"}
    else:
        result = mgr.check_and_acquire(args.workspace, args.task, engine)

    _sync_coding_stats()
    return result


@requires_workspace
def cmd_env_probe(args) -> dict:
    ws_path = args._ws_path

    config = ConfigManager()
    prober = EnvProber(config)
    extra = args.commands if hasattr(args, "commands") and args.commands else None

    def do_probe():
        result = prober.probe(args.env, extra_commands=extra)
        if result.get("ok") and result.get("data"):
            # Save artifact
            art_dir = Path(ws_path) / ARTIFACT_DIR
            art_dir.mkdir(exist_ok=True)
            snap_path = art_dir / "env_snapshot.json"
            snap_path.write_text(
                json.dumps(result["data"], indent=2, ensure_ascii=False)
            )
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact("env_snapshot", f"{ARTIFACT_DIR}/env_snapshot.json")
                lock.save()
        return result

    return with_lock_update(ws_path, "env-probe", do_probe)


def cmd_analyze(args) -> dict:
    config = ConfigManager()

    # Validate: at least one of --workspace or --repos must be provided
    if not getattr(args, "workspace", None) and not getattr(args, "repos", None):
        avail = _available_names(config)
        return {
            "ok": False,
            "error": "Either --workspace or --repos is required.",
            "error_code": "INVALID_ARGS",
            "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}.",
        }

    engine_name = args.engine or config.get_default_engine()
    engine = _get_engine(engine_name)
    if engine is None:
        return {"ok": False, "error": f"unknown engine: {engine_name}",
                "error_code": "ENGINE_ERROR"}

    max_turns = config.get_max_turns()

    # --repos mode: lock-free, read-only analysis directly on repo paths
    if getattr(args, "repos", None):
        resolved = _resolve_repo_paths(args, config)
        if isinstance(resolved, dict):
            return resolved  # error

        mgr = WorkspaceManager(config)
        repos_data = {}
        for name, repo_path in resolved:
            repo_cfg = config.get_repo(name)
            git_info = mgr._probe_git(repo_path)
            runtime = mgr._probe_runtime(repo_path)
            project = mgr._probe_project(repo_path, repo_cfg)
            repos_data[name] = {
                "path": repo_path,
                "git": git_info,
                "runtime": runtime,
                "project": project,
            }

        ws_snapshot = json.dumps(repos_data, indent=2, ensure_ascii=False)
        env_snapshot = "(not available — repos mode, no env probe)"

        prompt = ANALYZE_PROMPT.format(
            workspace_snapshot=ws_snapshot,
            env_snapshot=env_snapshot,
            task=args.task,
        )

        # Run engine on the first repo path (primary analysis target)
        primary_path = resolved[0][1]
        result = engine.run(primary_path, prompt, max_turns=max_turns)

        complexity = _parse_complexity(result.summary) if result.success else "standard"

        return {
            "ok": result.success,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
                "complexity": complexity,
                "feature_plan_created": False,
                "feature_count": 0,
            },
            **({"error": result.error, "error_code": "ENGINE_ERROR"} if result.error else {}),
        }

    # --workspace mode (original behavior, requires session/lock)
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        avail = _available_names(config)
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND",
                "hint": f"Available workspaces: {avail['workspaces']}. Available repos: {avail['repos']}."}
    session_path = Path(ws_path) / ARTIFACT_DIR / "session.json"
    if not session_path.exists():
        return {"ok": False,
                "error": "run workspace-check first to start a session",
                "error_code": "NO_SESSION"}
    session = json.loads(session_path.read_text())
    ws_path = session["ws_path"]

    ws_snapshot = _load_artifact(ws_path, "workspace_snapshot.json")
    env_snapshot = _load_artifact(ws_path, "env_snapshot.json")

    prompt = ANALYZE_PROMPT.format(
        workspace_snapshot=ws_snapshot,
        env_snapshot=env_snapshot,
        task=args.task,
    )

    def do_analyze():
        result = engine.run(ws_path, prompt, max_turns=max_turns)
        if result.success:
            # Save analysis artifact only on success
            art_dir = Path(ws_path) / ARTIFACT_DIR
            art_dir.mkdir(exist_ok=True)
            analysis_path = art_dir / "phase2_analysis.md"
            analysis_path.write_text(result.summary)
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact("analysis_report", f"{ARTIFACT_DIR}/phase2_analysis.md")
                lock.save()

        complexity = _parse_complexity(result.summary) if result.success else "standard"
        feature_plan_created = False
        feature_count = 0

        if result.success and complexity == "complex":
            feature_plan = _parse_feature_plan(result.summary)
            if feature_plan:
                fm = FeatureManager(ws_path)
                fm.create_plan_from_analysis(args.task, feature_plan)
                feature_plan_created = True
                feature_count = len(feature_plan)

        return {
            "ok": result.success,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
                "complexity": complexity,
                "feature_plan_created": feature_plan_created,
                "feature_count": feature_count,
            },
            **({"error": result.error, "error_code": "ENGINE_ERROR"} if result.error else {}),
        }

    return with_lock_update(ws_path, "analyzing", do_analyze)


@requires_workspace
def cmd_develop(args) -> dict:
    ws_path = args._ws_path

    config = ConfigManager()
    engine_name = args.engine or config.get_default_engine()
    engine = _get_engine(engine_name)
    if engine is None:
        return {"ok": False, "error": f"unknown engine: {engine_name}",
                "error_code": "ENGINE_ERROR"}

    max_turns = config.get_max_turns()
    ws_snapshot = _load_artifact(ws_path, "workspace_snapshot.json")
    analysis = _load_artifact(ws_path, "phase2_analysis.md")

    def do_develop():
        # Create branch
        if args.branch:
            git = GitOps(ws_path)
            br_result = git.create_branch(args.branch)
            if not br_result.get("ok"):
                return br_result
            # Update lock with branch name
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.data["branch"] = args.branch
                lock.save()

        prompt = DEVELOP_PROMPT.format(
            workspace_snapshot=ws_snapshot,
            analysis=analysis,
            plan=args.plan or "(proceed with recommended approach)",
            task=args.task,
            test_command="(no test command available)",
        )

        result = engine.run(ws_path, prompt, max_turns=max_turns)
        return {
            "ok": result.success,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
            },
            **({"error": result.error} if result.error else {}),
        }

    return with_lock_update(ws_path, "developing", do_develop)


@requires_workspace
def cmd_test(args) -> dict:
    ws_path = args._ws_path

    config = ConfigManager()
    runner = TestRunner(config)

    def do_test():
        result = runner.run(args.workspace)
        if result.get("ok"):
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact("test_report", f"{ARTIFACT_DIR}/test_report.json")
                lock.save()
        return result

    return with_lock_update(ws_path, "testing", do_test)


@requires_workspace
def cmd_submit_pr(args) -> dict:
    ws_path = args._ws_path

    # When --repo is provided, operate on the repo subdirectory within the workspace
    repo_name = getattr(args, "repo", None)
    if repo_name:
        config = ConfigManager()
        repo_info = config.get_repo(repo_name)
        if repo_info is None:
            return {"ok": False, "error": f"repo '{repo_name}' not found in config",
                    "error_code": "REPO_NOT_FOUND"}
        # Repo lives as a subdirectory inside the workspace
        repo_path = str(Path(ws_path) / repo_name)
        if not Path(repo_path).is_dir():
            return {"ok": False, "error": f"repo directory not found: {repo_path}",
                    "error_code": "PATH_NOT_FOUND",
                    "hint": f"Expected repo '{repo_name}' at {repo_path}. Check workspace layout."}
        git = GitOps(repo_path)
    else:
        git = GitOps(ws_path)

    def do_submit():
        result = git.submit_pr(
            title=args.title,
            body=args.body or "",
            commit_message=args.title,
        )
        # Track pushed_to_remote in lock
        if result.get("ok"):
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.data["pushed_to_remote"] = True
                lock.save()
        return result

    return with_lock_update(ws_path, "submitted", do_submit)


@requires_workspace
def cmd_env_verify(args) -> dict:
    ws_path = args._ws_path

    config = ConfigManager()
    prober = EnvProber(config)
    baseline_path = str(Path(ws_path) / ARTIFACT_DIR / "env_snapshot.json")

    def do_verify():
        result = prober.verify(args.env, baseline_path)
        if result.get("ok") and result.get("data"):
            # Save verification report
            art_dir = Path(ws_path) / ARTIFACT_DIR
            art_dir.mkdir(exist_ok=True)
            report_path = art_dir / "env_verify_report.json"
            report_path.write_text(
                json.dumps(result["data"], indent=2, ensure_ascii=False)
            )
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact(
                    "env_verify_report",
                    f"{ARTIFACT_DIR}/env_verify_report.json",
                )
                lock.save()
        return result

    return with_lock_update(ws_path, "env-verified", do_verify)


def cmd_auto_dev(args) -> dict:
    """One-step development: workspace-check → develop (with test loop) → final test → report.

    All repo resolution goes through repo_target.resolve_repo_target().
    All execution uses the resolved RepoTarget — never raw workspace paths.
    """
    from repo_target import (
        resolve_repo_target, resolve_repo_target_for_feature,
        run_final_test, RepoTargetBinding,
    )

    config = ConfigManager()
    mgr = WorkspaceManager(config)

    # ── 0. Feature mode ──
    feature_mode = getattr(args, "feature", None) == "next"
    current_feature = None

    if feature_mode:
        ws_name = getattr(args, "workspace", None)
        if not ws_name:
            return {
                "ok": False,
                "error": "--feature next requires --workspace",
                "error_code": "MISSING_WORKSPACE",
                "hint": "用 analyze 返回的 workspace 名称: $D auto-dev --workspace env0 --feature next",
            }
        ws = config.get_workspace(ws_name)
        if ws is None:
            return {"ok": False, "error": f"workspace '{ws_name}' not found",
                    "error_code": "NO_WORKSPACE"}

        fm = FeatureManager(ws["path"])
        feat = fm.next_feature()
        if not feat.get("ok"):
            _sync_coding_stats()
            return feat

        feat_data = feat.get("data", {})
        current_feature = feat_data.get("feature")
        if current_feature is None:
            status = feat_data.get("status", "unknown")
            if status == "all_complete":
                return {
                    "ok": True,
                    "data": {"status": "all_complete", "progress": feat_data.get("progress")},
                    "next_step": f"所有 feature 已完成。提交 PR: $D submit --workspace {ws_name} --title \"<title>\"",
                }
            return {"ok": False, "error": "no executable feature available",
                    "error_code": "NO_FEATURE",
                    "hint": "运行 $D feature-list 查看当前 plan 状态"}

    # ── 1. Task + complexity check ──
    task = current_feature["task"] if current_feature else getattr(args, "task", "")
    if not task:
        return {"ok": False, "error": "--task is required", "error_code": "INVALID_ARGS"}

    if not getattr(args, "allow_complex", False) and _looks_complex(task):
        repos_hint = getattr(args, "repos", "") or ""
        return {
            "ok": False,
            "error_code": "TASK_TOO_COMPLEX",
            "hint": f"建议先分析: $D analyze --repos {repos_hint} --task \"{task}\"",
        }

    # ── 2. Engine ──
    engine_name = getattr(args, "engine", None) or config.get_default_engine()
    engine = _get_engine(engine_name)
    if engine is None:
        return {"ok": False, "error": f"unknown engine: {engine_name}",
                "error_code": "ENGINE_ERROR"}

    # ── 3. Resolve repo target (unified) ──
    if feature_mode:
        binding = resolve_repo_target_for_feature(
            config=config,
            workspace_arg=getattr(args, "workspace", None),
            repo_arg=getattr(args, "repo", None),
            feature=current_feature,
        )
    else:
        binding = resolve_repo_target(
            config=config,
            repos_arg=getattr(args, "repos", None),
            workspace_arg=getattr(args, "workspace", None),
            repo_arg=getattr(args, "repo", None),
        )

    # Handle NEEDS_WORKSPACE_ACQUISITION — acquire workspace then re-resolve
    if isinstance(binding, dict) and binding.get("error_code") == "NEEDS_WORKSPACE_ACQUISITION":
        repo_names = binding["data"]["repo_names"]
        resolved_repo_name = binding["data"]["repo_name"]
        ws_result = mgr.check_and_acquire_for_repos(repo_names, task, engine_name)
        if not ws_result.get("ok"):
            return ws_result
        # Re-resolve with acquired workspace
        acquired_ws_name = ws_result["data"]["snapshot"]["workspace"]["name"]
        binding = resolve_repo_target(
            config=config,
            workspace_arg=acquired_ws_name,
            repo_arg=getattr(args, "repo", None) or resolved_repo_name,
        )

    if isinstance(binding, dict):
        return binding  # error

    ws_ctx = binding.workspace
    target = binding.target

    # ── 3.5. Reset worktree if requested ──
    if getattr(args, "reset_worktree", False):
        snapshot_text = _load_artifact(ws_ctx.path, "workspace_snapshot.json")
        clean_result = _clean_workspace_repos(ws_ctx.path, snapshot_text)
        if not clean_result.get("ok"):
            return clean_result

    # ── 4. Build prompt + run engine (uses only RepoTarget) ──
    ws_snapshot = _load_artifact(ws_ctx.path, "workspace_snapshot.json")
    analysis = _load_artifact(ws_ctx.path, "phase2_analysis.md")

    prompt = DEVELOP_PROMPT.format(
        workspace_snapshot=ws_snapshot,
        analysis=analysis,
        plan=current_feature.get("plan", "") if current_feature else getattr(args, "plan", None) or "(proceed with recommended approach)",
        task=task,
        test_command=target.test_command or "(no test command available)",
    )

    max_turns = config.get_max_turns()

    def do_dev():
        # Branch
        branch = getattr(args, "branch", None)
        if branch:
            git = GitOps(target.git_root)
            br_result = git.create_branch(branch)
            if not br_result.get("ok"):
                return br_result
            lock = LockFile(ws_ctx.path)
            if lock.exists():
                lock.load()
                lock.data["branch"] = branch
                lock.save()

        # Engine runs on RepoTarget.repo_path
        result = engine.run(target.repo_path, prompt, max_turns=max_turns)
        if not result.success:
            return {
                "ok": False,
                "error": result.error or "engine failed",
                "error_code": "ENGINE_ERROR",
                "hint": "引擎失败，可换引擎重试: --engine codex 或 --engine claude",
            }

        # ── Final test (uses RepoTarget) ──
        test_status = run_final_test(target, config)

        if test_status.status == "failed":
            return {
                "ok": False,
                "error": "final verification failed after engine completed development",
                "error_code": "FINAL_TEST_FAILED",
                "data": {
                    "summary": result.summary,
                    "files_changed": result.files_changed,
                    "test_status": test_status.status,
                    "test_reason": test_status.reason,
                    "test_report": test_status.report,
                },
                "hint": "查看失败报告后重试 auto-dev，或手动运行 $D test / $D analyze",
            }

        # Feature done
        if feature_mode and current_feature is not None:
            fm = FeatureManager(ws_ctx.path)
            fm.mark_done(index=current_feature["index"], force=False)

        return {
            "ok": True,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
                "test_status": test_status.status,
                "test_reason": test_status.reason,
                "test_report": test_status.report,
            },
            "next_step": _suggest_next(args, feature_mode=feature_mode, tests_passed=test_status.passed),
        }

    result = with_lock_update(ws_ctx.path, "developing", do_dev)
    _sync_coding_stats()
    return result


def cmd_submit(args) -> dict:
    """Simplified submit: supports both --repos and --workspace. Auto-releases on success.

    All repo resolution goes through repo_target module.
    """
    from repo_target import (
        resolve_repo_target, find_active_workspaces_by_repos, RepoTargetBinding,
    )

    config = ConfigManager()

    # ── Resolve target (unified) ──
    repos_arg = getattr(args, "repos", None)
    workspace_arg = getattr(args, "workspace", None)
    repo_arg = getattr(args, "repo", None)

    if not workspace_arg and repos_arg:
        # Use formal repo→workspace finder
        repo_names = [r.strip() for r in repos_arg.split(",") if r.strip()]
        matches = find_active_workspaces_by_repos(config, repo_names)

        if len(matches) == 0:
            return {
                "ok": False,
                "error": f"no active workspace found for repos: {repos_arg}",
                "error_code": "NO_WORKSPACE",
                "hint": "先运行 auto-dev 创建 workspace，或用 --workspace 指定",
            }
        if len(matches) > 1:
            return {
                "ok": False,
                "error": "multiple active workspaces found",
                "error_code": "AMBIGUOUS_WORKSPACE",
                "hint": "请显式传 --workspace，避免提交到错误分支",
            }
        workspace_arg = matches[0]["name"]
        if len(repo_names) == 1 and not repo_arg:
            repo_arg = repo_names[0]

    if not workspace_arg:
        return {"ok": False, "error": "either --workspace or --repos is required",
                "error_code": "INVALID_ARGS"}

    binding = resolve_repo_target(
        config=config,
        workspace_arg=workspace_arg,
        repo_arg=repo_arg,
    )
    if isinstance(binding, dict):
        return binding  # error

    ws_ctx = binding.workspace
    target = binding.target

    # ── Submit PR using RepoTarget ──
    git = GitOps(target.git_root)

    def do_submit():
        result = git.submit_pr(
            title=args.title,
            body=getattr(args, "body", "") or "",
            commit_message=args.title,
        )
        if result.get("ok"):
            lock = LockFile(ws_ctx.path)
            if lock.exists():
                lock.load()
                lock.data["pushed_to_remote"] = True
                lock.save()
        return result

    result = with_lock_update(ws_ctx.path, "submitted", do_submit)

    # Auto-release on success unless --keep-lock
    if result.get("ok") and not getattr(args, "keep_lock", False):
        import argparse as _ap
        release_args = _ap.Namespace(workspace=workspace_arg, **{"all": False}, cleanup=False)
        cmd_release(release_args)

    _sync_coding_stats()
    return result


def cmd_release(args) -> dict:
    config = ConfigManager()
    mgr = WorkspaceManager(config)
    cleanup = getattr(args, "cleanup", False)
    if getattr(args, "all", False):
        result = mgr.release_all(cleanup=cleanup)
    elif not args.workspace:
        result = {"ok": False, "error": "--workspace or --all is required",
                "error_code": "INVALID_ARGS"}
    else:
        result = mgr.release(args.workspace, cleanup=cleanup)
    _sync_coding_stats()
    return result


def cmd_renew_lease(args) -> dict:
    config = ConfigManager()
    mgr = WorkspaceManager(config)
    return mgr.renew_lease(args.workspace)


# ── Feature management ───────────────────────────────────

@requires_workspace
def cmd_feature_plan(args) -> dict:
    ws_path = args._ws_path
    fm = FeatureManager(ws_path)
    features = json.loads(args.features)
    return fm.create_plan(args.task, features)


@requires_workspace
def cmd_feature_next(args) -> dict:
    ws_path = args._ws_path
    fm = FeatureManager(ws_path)
    return fm.next_feature()


@requires_workspace
def cmd_feature_done(args) -> dict:
    ws_path = args._ws_path
    fm = FeatureManager(ws_path)
    return fm.mark_done(
        index=args.index,
        branch=getattr(args, "branch", None),
        pr=getattr(args, "pr", None),
        force=getattr(args, "force", False),
    )


def cmd_feature_list(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}
    fm = FeatureManager(ws_path)
    return fm.list_all()


@requires_workspace
def cmd_feature_criteria(args) -> dict:
    ws_path = args._ws_path
    fm = FeatureManager(ws_path)
    new_criteria = None
    if getattr(args, "criteria", None):
        new_criteria = json.loads(args.criteria)
        if isinstance(new_criteria, dict):
            new_criteria = [new_criteria]
    return fm.criteria(
        index=args.index,
        action=args.action,
        new_criteria=new_criteria,
    )


@requires_workspace
def cmd_feature_verify(args) -> dict:
    ws_path = args._ws_path

    engine = None
    engine_name = getattr(args, "engine", None)
    if engine_name:
        engine = _get_engine(engine_name)

    fm = FeatureManager(ws_path)
    return fm.verify(
        index=args.index,
        workspace=ws_path,
        engine=engine,
    )


@requires_workspace
def cmd_feature_update(args) -> dict:
    ws_path = args._ws_path
    fm = FeatureManager(ws_path)
    return fm.update(
        index=args.index,
        status=getattr(args, "status", None),
        title=getattr(args, "title", None),
        task=getattr(args, "task_desc", None),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Argument parser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dispatch.py", description="Coding Master CLI")
    sub = p.add_subparsers(dest="command", required=True)

    # ── Config ──────────────────────────────────────────────
    sub.add_parser("config-list", help="List all config")

    ca = sub.add_parser("config-add", help="Add workspace or env")
    ca.add_argument("kind", choices=["repo", "workspace", "env"])
    ca.add_argument("name")
    ca.add_argument("value")

    cs = sub.add_parser("config-set", help="Set a field on workspace or env")
    cs.add_argument("kind", choices=["repo", "workspace", "env"])
    cs.add_argument("name")
    cs.add_argument("key")
    cs.add_argument("value")

    cr = sub.add_parser("config-remove", help="Remove workspace or env")
    cr.add_argument("kind", choices=["repo", "workspace", "env"])
    cr.add_argument("name")

    # ── Quick queries (lock-free) ────────────────────────────
    qs = sub.add_parser("quick-status", help="Workspace overview (lock-free)")
    qs.add_argument("--workspace", default=None, help="Workspace slot name")
    qs.add_argument("--repos", default=None, help="Comma-separated repo names for direct status check")

    qt = sub.add_parser("quick-test", help="Run tests without lock")
    qt.add_argument("--workspace", default=None, help="Workspace slot name")
    qt.add_argument("--repos", default=None, help="Comma-separated repo names for direct test run")
    qt.add_argument("--path", default=None, help="Specific test path/directory")
    qt.add_argument("--lint", action="store_true", help="Also run lint")

    qf = sub.add_parser("quick-find", help="Search code in workspace or repo")
    qf.add_argument("--workspace", default=None, help="Workspace slot name (env0/env1/env2)")
    qf.add_argument("--repos", default=None, help="Comma-separated repo names for direct source search")
    qf.add_argument("--query", required=True, help="Search pattern (grep)")
    qf.add_argument("--glob", default=None, help="File pattern filter")

    qe = sub.add_parser("quick-env", help="Probe env without workspace (lock-free)")
    qe.add_argument("--env", required=True)
    qe.add_argument("--commands", nargs="*", default=None)

    # ── Aliases for simplified interface ─────────────────────
    sub.add_parser("status", help="Alias for quick-status").add_argument("--workspace", default=None)
    sub._name_parser_map["status"].add_argument("--repos", default=None)

    sub.add_parser("find", help="Alias for quick-find").add_argument("--workspace", default=None)
    sub._name_parser_map["find"].add_argument("--repos", default=None)
    sub._name_parser_map["find"].add_argument("--query", required=True)
    sub._name_parser_map["find"].add_argument("--glob", default=None)

    # ── auto-dev ──────────────────────────────────────────
    ad = sub.add_parser("auto-dev", help="One-step development: workspace-check + develop + test")
    ad.add_argument("--repos", default=None, help="Comma-separated repo names (single repo only)")
    ad.add_argument("--workspace", default=None, help="Use existing workspace")
    ad.add_argument("--task", default=None, help="Task description")
    ad.add_argument("--branch", default=None, help="Branch name (auto-generated if omitted)")
    ad.add_argument("--engine", default=None, help="Engine: claude or codex")
    ad.add_argument("--feature", default=None, help="'next' to develop next feature from plan")
    ad.add_argument("--repo", default=None, help="Target repo in multi-repo workspace")
    ad.add_argument("--plan", default=None, help="Implementation plan")
    ad.add_argument("--allow-complex", action="store_true", help="Skip complexity check")
    ad.add_argument("--reset-worktree", action="store_true", help="Clean uncommitted changes before developing")

    # ── submit ────────────────────────────────────────────
    sm = sub.add_parser("submit", help="Commit, push, create PR (auto-releases workspace)")
    sm.add_argument("--workspace", default=None, help="Workspace name")
    sm.add_argument("--repos", default=None, help="Comma-separated repo names")
    sm.add_argument("--repo", default=None, help="Repo name within workspace")
    sm.add_argument("--title", required=True, help="PR title")
    sm.add_argument("--body", default="", help="PR body")
    sm.add_argument("--keep-lock", action="store_true", help="Don't auto-release workspace after submit")

    # ── Workflow ────────────────────────────────────────────
    wc = sub.add_parser("workspace-check", help="Check and acquire workspace")
    wc.add_argument("--workspace", default=None, help="Workspace name (required in direct mode, optional with --repos)")
    wc.add_argument("--task", required=True)
    wc.add_argument("--engine", default=None)
    wc.add_argument("--repos", default=None, help="Comma-separated repo names (auto-allocates workspace if --workspace omitted)")
    wc.add_argument("--auto-clean", action="store_true", help="Auto-reset dirty workspace repos (git reset --hard + clean -fd)")

    ep = sub.add_parser("env-probe", help="Probe runtime environment")
    ep.add_argument("--workspace", default=None, help="Workspace name (auto-detected if only one active)")
    ep.add_argument("--env", required=True)
    ep.add_argument("--commands", nargs="*", default=None)

    az = sub.add_parser("analyze", help="Analyze issue with coding engine")
    az.add_argument("--workspace", default=None, help="Workspace slot name (required for workspace mode)")
    az.add_argument("--repos", default=None, help="Comma-separated repo names for lock-free direct analysis")
    az.add_argument("--task", required=True)
    az.add_argument("--engine", default=None)

    dv = sub.add_parser("develop", help="Develop fix with coding engine")
    dv.add_argument("--workspace", default=None, help="Workspace name (auto-detected if only one active)")
    dv.add_argument("--task", required=True)
    dv.add_argument("--plan", default=None)
    dv.add_argument("--branch", default=None)
    dv.add_argument("--engine", default=None)

    ts = sub.add_parser("test", help="Run lint + tests")
    ts.add_argument("--workspace", default=None, help="Workspace name (auto-detected if only one active)")

    sp = sub.add_parser("submit-pr", help="Commit, push, create PR")
    sp.add_argument("--workspace", default=None, help="Workspace name (auto-detected if only one active)")
    sp.add_argument("--repo", default=None, help="Repo name within workspace (e.g. 'alfred'). Uses workspace root if omitted.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", default="")

    ev = sub.add_parser("env-verify", help="Verify fix in deployment env")
    ev.add_argument("--workspace", default=None, help="Workspace name (auto-detected if only one active)")
    ev.add_argument("--env", required=True)

    rl = sub.add_parser("release", help="Release workspace lock")
    rl.add_argument("--workspace", default=None, help="Workspace name (required unless --all)")
    rl.add_argument("--all", action="store_true", help="Release all workspace locks")
    rl.add_argument("--cleanup", action="store_true")

    rn = sub.add_parser("renew-lease", help="Renew workspace lock lease")
    rn.add_argument("--workspace", default=None, help="Workspace name (auto-detected if only one active)")

    # ── Feature management ──────────────────────────────────
    _ws_help = "Workspace name (auto-detected if only one active)"

    fp = sub.add_parser("feature-plan", help="Create feature split plan")
    fp.add_argument("--workspace", default=None, help=_ws_help)
    fp.add_argument("--task", required=True)
    fp.add_argument("--features", required=True, help="JSON array of {title, task, depends_on?}")

    fn = sub.add_parser("feature-next", help="Get next executable feature")
    fn.add_argument("--workspace", default=None, help=_ws_help)

    fd = sub.add_parser("feature-done", help="Mark feature as done")
    fd.add_argument("--workspace", default=None, help=_ws_help)
    fd.add_argument("--index", type=int, required=True)
    fd.add_argument("--branch", default=None)
    fd.add_argument("--pr", default=None)
    fd.add_argument("--force", action="store_true", help="Skip criteria check")

    fl = sub.add_parser("feature-list", help="List all features and status")
    fl.add_argument("--workspace", default=None, help=_ws_help)

    fu = sub.add_parser("feature-update", help="Update a feature")
    fu.add_argument("--workspace", default=None, help=_ws_help)
    fu.add_argument("--index", type=int, required=True)
    fu.add_argument("--status", default=None, choices=["pending", "in_progress", "done", "skipped"])
    fu.add_argument("--title", default=None)
    fu.add_argument("--task-desc", default=None)

    fc = sub.add_parser("feature-criteria", help="View/append feature acceptance criteria")
    fc.add_argument("--workspace", default=None, help=_ws_help)
    fc.add_argument("--index", type=int, required=True)
    fc.add_argument("--action", required=True, choices=["view", "append"])
    fc.add_argument("--criteria", default=None, help="JSON criteria to append (single object or array)")

    fv = sub.add_parser("feature-verify", help="Run feature acceptance criteria verification")
    fv.add_argument("--workspace", default=None, help=_ws_help)
    fv.add_argument("--index", type=int, required=True)
    fv.add_argument("--engine", default=None, help="Engine for assert-type criteria")

    return p


COMMANDS = {
    "config-list": cmd_config_list,
    "config-add": cmd_config_add,
    "config-set": cmd_config_set,
    "config-remove": cmd_config_remove,
    # Simplified aliases
    "status": cmd_quick_status,
    "find": cmd_quick_find,
    # Original names (kept for backward compatibility)
    "quick-status": cmd_quick_status,
    "quick-test": cmd_quick_test,
    "quick-find": cmd_quick_find,
    "quick-env": cmd_quick_env,
    # New commands
    "auto-dev": cmd_auto_dev,
    "submit": cmd_submit,
    # Workflow
    "workspace-check": cmd_workspace_check,
    "env-probe": cmd_env_probe,
    "analyze": cmd_analyze,
    "develop": cmd_develop,
    "test": cmd_test,
    "submit-pr": cmd_submit_pr,
    "env-verify": cmd_env_verify,
    "release": cmd_release,
    "renew-lease": cmd_renew_lease,
    # Feature management
    "feature-plan": cmd_feature_plan,
    "feature-next": cmd_feature_next,
    "feature-done": cmd_feature_done,
    "feature-list": cmd_feature_list,
    "feature-update": cmd_feature_update,
    "feature-criteria": cmd_feature_criteria,
    "feature-verify": cmd_feature_verify,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Workspace status & CODING.md sync
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _collect_workspace_status() -> list[dict]:
    """Scan all configured workspaces, return status of active (locked) ones."""
    try:
        config = ConfigManager()
    except Exception:
        return []
    all_ws = config._section().get("workspaces", {})
    active = []
    for name in all_ws:
        ws = config.get_workspace(name)
        if ws is None:
            continue
        lock = LockFile(ws["path"])
        if not lock.exists():
            continue
        try:
            lock.load()
        except Exception:
            continue
        if lock.is_expired():
            continue
        active.append({
            "name": name,
            "task": lock.data.get("task", ""),
            "branch": lock.data.get("branch"),
            "phase": lock.data.get("phase", ""),
            "engine": lock.data.get("engine", ""),
            "path": lock.data.get("ws_path", ws["path"]),
            "updated_at": lock.data.get("updated_at", ""),
        })
    return active


def _sync_coding_stats() -> None:
    """Write CODING_STATS.md to agent_dir based on current workspace status.

    This file is fully owned by dispatch — agent should NOT edit it.
    Agent-owned notes go into CODING.md (which dispatch never touches).
    """
    try:
        config = ConfigManager()
        agent_dir = config.get_agent_dir()
        if not agent_dir:
            return
        agent_path = Path(agent_dir)
        if not agent_path.is_dir():
            return

        stats_path = agent_path / "CODING_STATS.md"
        active = _collect_workspace_status()
        expired = _collect_expired_workspace_status()

        if not active and not expired:
            if stats_path.exists():
                stats_path.unlink()
            return

        lines = []

        for ws in active:
            lines.append(f"## {ws['name']} — {ws['phase']} ✅ active")
            _append_workspace_details(lines, ws)

        for ws in expired:
            lines.append(f"## {ws['name']} — ⚠️ lease expired")
            _append_workspace_details(lines, ws)
            lines.append("- **Status**: lease expired，需要 `$D workspace-check` 重新获取锁后才能继续开发")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Rules")
        lines.append("")
        lines.append(
            "- **所有代码开发、测试、搜索操作必须通过 coding-master skill 的 dispatch 命令执行**"
            "（`$D auto-dev`, `$D test`, `$D status`, `$D find` 等），"
            "**禁止**直接用 `_bash` 拼 `cd ... && pytest/pip install/python -m pytest` 命令。"
        )
        lines.append(
            "- dispatch 会自动处理正确的 Python 解释器、pytest 路径、"
            "依赖安装和 workspace 上下文，裸 bash 无法保证这些。"
        )
        lines.append(
            "- 标准开发流程: `$D auto-dev --repos <name> --task \"...\"` 一步完成开发+测试。"
            "测试通过后: `$D submit --repos <name> --title \"...\"`。"
        )
        lines.append("")

        stats_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass  # best-effort, never break the main command


def _append_workspace_details(lines: list[str], ws: dict) -> None:
    """Append workspace detail lines (shared between active and expired)."""
    lines.append(f"- **Task**: {ws['task']}")
    if ws.get("branch"):
        lines.append(f"- **Branch**: {ws['branch']}")
    lines.append(f"- **Engine**: {ws.get('engine', '')}")
    lines.append(f"- **Path**: {ws['path']}")
    if ws.get("updated_at"):
        lines.append(f"- **Updated**: {ws['updated_at']}")

    # Enrich from artifacts if available
    ws_path = ws["path"]
    test_report = _load_artifact_or_none(ws_path, "test_report.json")
    if test_report:
        try:
            report = json.loads(test_report)
            overall = report.get("overall", "?")
            test = report.get("test", {})
            lines.append(
                f"- **Last test**: {overall} "
                f"({test.get('passed_count', 0)} passed, {test.get('failed_count', 0)} failed)"
            )
        except (json.JSONDecodeError, TypeError):
            pass

    git_dirty = _quick_git_stat(ws_path)
    if git_dirty:
        lines.append(f"- **Uncommitted changes**: {git_dirty}")

    lines.append("")


def _collect_expired_workspace_status() -> list[dict]:
    """Scan workspaces with expired (but existing) locks."""
    try:
        config = ConfigManager()
    except Exception:
        return []
    all_ws = config._section().get("workspaces", {})
    expired = []
    for name in all_ws:
        ws = config.get_workspace(name)
        if ws is None:
            continue
        lock = LockFile(ws["path"])
        if not lock.exists():
            continue
        try:
            lock.load()
        except Exception:
            continue
        if not lock.is_expired():
            continue
        expired.append({
            "name": name,
            "task": lock.data.get("task", ""),
            "branch": lock.data.get("branch"),
            "phase": lock.data.get("phase", ""),
            "engine": lock.data.get("engine", ""),
            "path": lock.data.get("ws_path", ws["path"]),
            "updated_at": lock.data.get("updated_at", ""),
        })
    return expired


def _load_artifact_or_none(ws_path: str, filename: str) -> str | None:
    """Read an artifact file from .coding-master/ dir, return content or None."""
    art = Path(ws_path) / ARTIFACT_DIR / filename
    if art.is_file():
        try:
            return art.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _quick_git_stat(ws_path: str) -> str:
    """Return a short git diff --stat summary, or empty string."""
    try:
        r = subprocess.run(
            ["git", "diff", "--stat", "--no-color"],
            cwd=ws_path, capture_output=True, text=True, timeout=10,
        )
        lines = r.stdout.strip().splitlines()
        if lines:
            return lines[-1].strip()  # e.g. "3 files changed, 50 insertions(+), 10 deletions(-)"
    except Exception:
        pass
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if handler is None:
        result = {"ok": False, "error": f"unknown command: {args.command}"}
    else:
        try:
            result = handler(args)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}

    # Inject active workspace status into every response
    if isinstance(result, dict):
        ws_status = _collect_workspace_status()
        if ws_status:
            result["_workspaces"] = ws_status

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
