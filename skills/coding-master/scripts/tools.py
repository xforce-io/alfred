#!/usr/bin/env python3
"""Coding Master v3 — minimal tooling for convention-driven development.

Each tool does one mechanical thing, no orchestration.
JSON files use flock for atomicity.
Parallel development via per-feature worktree isolation.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import logging
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── Add scripts dir to path so we can import siblings ──
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config_manager import ConfigManager

CM_DIR = ".coding-master"
EVIDENCE_DIR = "evidence"
DELEGATION_DIR = "delegation"
LEASE_MINUTES = 120
READ_ONLY_MODES = {"review", "analyze"}  # These modes must not modify git state
TEST_OUTPUT_MAX = 500

# ── Mode definitions: constraints, not pipelines ──
MODES = {
    "deliver": {
        "required_artifacts": [],  # checked per-feature via evidence/N-verify.json
        "allowed_commands": [
            "lock", "unlock", "status", "renew", "start",
            "plan-ready", "claim", "dev", "test", "done", "reopen",
            "integrate", "submit", "progress", "journal", "doctor",
            "delegate-prepare", "delegate-complete",
            "engine-run",
        ],
        "completion_gate": "all features done + evidence pass",
        "description": "Feature delivery: plan → claim → dev → test → done → submit",
    },
    "review": {
        "required_artifacts": ["scope.json", "report.md"],
        "allowed_commands": [
            "lock", "unlock", "status", "renew", "start",
            "scope", "report", "progress", "journal", "doctor",
            "delegate-prepare", "delegate-complete",
            "engine-run",
        ],
        "completion_gate": "report.md exists",
        "description": "Code review: structured analysis and feedback",
    },
    "debug": {
        "required_artifacts": ["scope.json", "diagnosis.md"],
        "allowed_commands": [
            "lock", "unlock", "status", "renew", "start",
            "scope", "report", "progress", "journal", "doctor",
            "delegate-prepare", "delegate-complete",
            "dev", "test",  # debug may need code changes
            "engine-run",
        ],
        "completion_gate": "diagnosis.md exists",
        "description": "Debug: investigate, diagnose, optionally fix",
    },
    "analyze": {
        "required_artifacts": ["scope.json", "report.md"],
        "allowed_commands": [
            "lock", "unlock", "status", "renew", "start",
            "scope", "report", "progress", "journal", "doctor",
            "delegate-prepare", "delegate-complete",
            "engine-run",
        ],
        "completion_gate": "report.md exists",
        "description": "Analysis: understand code, produce structured conclusions",
    },
}

FEATURE_TEMPLATE = """\
# Feature {id}: {title}

## Spec
{task}

**Acceptance Criteria**:
{criteria}

## Analysis

## Plan

## Test Results

## Dev Log
"""

# ══════════════════════════════════════════════════════════
#  Atomic JSON operations
# ══════════════════════════════════════════════════════════


def _atomic_json_update(path: Path, updater: Callable[[dict], dict]) -> dict:
    """flock + read-modify-write. Rollback on updater failure (ok=False).

    updater receives mutable data dict, returns result dict.
    If result["ok"] is False, file is restored from deepcopy snapshot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            content = f.read()
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                data = {}
            snapshot = copy.deepcopy(data)
            result = updater(data)
            if result.get("ok", True):
                f.seek(0)
                f.truncate()
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                if data != snapshot:
                    f.seek(0)
                    f.truncate()
                    json.dump(snapshot, f, indent=2, ensure_ascii=False)
            return result
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _atomic_json_read(path: Path) -> dict:
    """flock-protected read. Returns {} if file missing or empty."""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            content = f.read()
            return json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ══════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════


def _repo_path(repo_name: str) -> Path:
    """Resolve repo name → local path via config."""
    cfg = ConfigManager()
    ws = cfg.get_workspace(repo_name)
    if ws and ws.get("path"):
        return Path(ws["path"]).resolve()
    repo = cfg.get_repo(repo_name)
    if repo and repo.get("path"):
        return Path(repo["path"]).resolve()
    # Fallback: check if cwd is the repo
    cwd = Path.cwd()
    if (cwd / ".git").exists():
        return cwd
    _fail(f"repo '{repo_name}' not found in config and cwd is not a git repo")


def _resolve_agent(args) -> str:
    if getattr(args, "agent", None):
        return args.agent
    return f"{socket.gethostname()}-{os.getpid()}"


def _is_expired(lock: dict) -> bool:
    exp = lock.get("lease_expires_at")
    if not exp:
        return False
    return datetime.fromisoformat(exp) < datetime.now(timezone.utc)


def _check_lease(repo: Path) -> dict:
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if not lock:
        return {"ok": False, "error": "no active lock"}
    if _is_expired(lock):
        # Auto-renew expired lease instead of blocking — enables cross-turn continuity
        _auto_renew_lease(repo)
    return {"ok": True}


def _auto_renew_lease(repo: Path) -> None:
    """Auto-renew an expired lease, preserving all session state."""
    lock_path = repo / CM_DIR / "lock.json"

    def do_renew(data):
        if not data:
            return {"ok": True}
        now = datetime.now(timezone.utc)
        data["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
        return {"ok": True}

    _atomic_json_update(lock_path, do_renew)


def _resolve_locked_repo(args) -> Path:
    """Resolve repo and verify lock exists."""
    repo = _repo_path(args.repo)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if not lock:
        _fail("no active lock. Run cm lock first")
    return repo


def _get_session_worktree(repo: Path, lock: dict | None = None) -> Path | None:
    """Return session worktree path from lock.json, or None if not set."""
    if lock is None:
        lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    wt = lock.get("session_worktree", "")
    if wt and Path(wt).exists():
        return Path(wt)
    return None


def _session_worktree_path(repo: Path) -> Path:
    """Return the canonical session worktree path for a repo."""
    return repo.parent / f"{repo.name}-session"


def _ensure_session_worktree(repo: Path, lock: dict | None = None) -> dict:
    """Ensure the session worktree exists for the active write session."""
    lock_path = repo / CM_DIR / "lock.json"
    if lock is None:
        lock = _atomic_json_read(lock_path)

    if not lock or lock.get("read_only", False):
        return {"ok": True, "data": {"session_worktree": ""}}

    branch = lock.get("branch", "")
    if not branch:
        return {"ok": False, "error": "lock references no branch for session worktree recovery"}

    recorded_wt = lock.get("session_worktree", "")
    session_wt = Path(recorded_wt) if recorded_wt else _session_worktree_path(repo)

    if recorded_wt and session_wt.exists():
        return {"ok": True, "data": {"session_worktree": str(session_wt)}}

    if session_wt.exists():
        _remove_worktree(repo, str(session_wt))

    branch_exists = _run_git(repo, ["rev-parse", "--verify", branch], check=False).returncode == 0
    worktree_cmd = ["worktree", "add", str(session_wt), branch] if branch_exists else ["worktree", "add", str(session_wt), "-b", branch]

    try:
        _run_git(repo, worktree_cmd)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        err = getattr(exc, "stderr", "") or str(exc)
        return {"ok": False, "error": f"session worktree recovery failed: {err}"}

    _atomic_json_update(lock_path, lambda d: (
        d.update({"session_worktree": str(session_wt)}), {"ok": True}
    )[1])
    return {"ok": True, "data": {"session_worktree": str(session_wt)}}


def _ensure_gitignore(repo: Path):
    """Ensure .coding-master/ is in .gitignore."""
    gi = repo / ".gitignore"
    marker = ".coding-master/"
    if gi.exists():
        content = gi.read_text()
        if marker in content:
            return
        gi.write_text(content.rstrip() + f"\n{marker}\n.coding-master.lock\n")
    else:
        gi.write_text(f"{marker}\n.coding-master.lock\n")


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower().strip())
    s = re.sub(r"[\s_]+", "-", s)
    return s[:30] or "feature"


def _run_git(repo: Path, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, return CompletedProcess."""
    return subprocess.run(
        ["git"] + cmd, cwd=repo, capture_output=True, text=True,
        check=check, timeout=120,
    )


def _write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _delegation_feature_dir(repo: Path, feature_id: str) -> Path:
    return repo / CM_DIR / DELEGATION_DIR / feature_id


def _delegation_request_path(repo: Path, feature_id: str) -> Path:
    return _delegation_feature_dir(repo, feature_id) / "request.json"


def _delegation_result_path(repo: Path, feature_id: str) -> Path:
    return _delegation_feature_dir(repo, feature_id) / "result.json"


def _delegation_behavior_summary_path(repo: Path, feature_id: str) -> Path:
    return _delegation_feature_dir(repo, feature_id) / "behavior_summary.md"


def _delegation_edge_case_matrix_path(repo: Path, feature_id: str) -> Path:
    return _delegation_feature_dir(repo, feature_id) / "edge_case_matrix.json"


def _delegation_artifacts_ready(repo: Path, feature_id: str, delegation: dict) -> tuple[bool, list[str]]:
    """Check whether delegation artifacts are complete for execute unlock."""
    task_type = delegation.get("task_type")
    required = [_delegation_result_path(repo, feature_id)]
    if task_type == "analyze-implementation":
        required.extend(
            [
                _delegation_behavior_summary_path(repo, feature_id),
                _delegation_edge_case_matrix_path(repo, feature_id),
            ]
        )
    missing = [str(path) for path in required if not path.exists()]
    return len(missing) == 0, missing


def _delegation_gate_error(feature_id: str, delegation: dict) -> dict:
    """Build a structured hard-gate error for execute commands."""
    return {
        "ok": False,
        "error": "delegation required before execute",
        "data": {
            "feature": feature_id,
            "task_type": delegation.get("task_type"),
            "reason": delegation.get("reason"),
            "status": delegation.get("status"),
        },
    }


def _check_delegation_gate(repo: Path, feature_id: str) -> dict | None:
    """Return hard-gate error when execute commands are blocked by must_delegate."""
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    feature = claims.get("features", {}).get(feature_id, {})
    if not feature:
        return None
    delegation = feature.get("delegation", {})
    if delegation.get("required") and delegation.get("status") != "completed":
        return _delegation_gate_error(feature_id, delegation)
    return None


def _get_session_mode(repo: Path) -> str:
    """Read session mode from lock.json, default to 'deliver'."""
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    return lock.get("mode", "deliver")


def _check_mode_gate(repo: Path, command: str) -> dict | None:
    """Return error if command is not allowed in the current session mode."""
    mode = _get_session_mode(repo)
    mode_def = MODES.get(mode)
    if not mode_def:
        return None  # unknown mode, allow everything
    if command not in mode_def["allowed_commands"]:
        return {
            "ok": False,
            "error": f"command '{command}' not available in '{mode}' mode",
            "data": {
                "mode": mode,
                "allowed_commands": mode_def["allowed_commands"],
                "description": mode_def["description"],
            },
        }
    return None


def _get_mode_artifact_status(repo: Path) -> dict:
    """Check which required artifacts exist for the current mode."""
    mode = _get_session_mode(repo)
    mode_def = MODES.get(mode, MODES["deliver"])
    required = mode_def["required_artifacts"]
    status = {}
    for artifact in required:
        path = repo / CM_DIR / artifact
        status[artifact] = "exists" if path.exists() else "missing"
    return status


def _check_mode_completion(repo: Path) -> tuple[bool, list[str]]:
    """Check if all required artifacts exist for session completion."""
    status = _get_mode_artifact_status(repo)
    missing = [name for name, st in status.items() if st == "missing"]
    return len(missing) == 0, missing


def _find_feature_md(repo: Path, feature_id: str) -> Path | None:
    """Find the feature MD file by feature ID prefix."""
    features_dir = repo / CM_DIR / "features"
    if not features_dir.exists():
        return None
    prefix = feature_id.zfill(2) + "-"
    for f in sorted(features_dir.iterdir()):
        if f.name.startswith(prefix) and f.suffix == ".md":
            return f
    return None


def _check_feature_md_sections(path: Path | None) -> tuple[bool, bool]:
    """Check if Analysis and Plan sections have content."""
    if not path or not path.exists():
        return False, False
    text = path.read_text()
    analysis_match = re.search(
        r"^## Analysis\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    plan_match = re.search(
        r"^## Plan\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    has_analysis = bool(analysis_match and analysis_match.group(1).strip())
    has_plan = bool(plan_match and plan_match.group(1).strip())
    return has_analysis, has_plan


def _get_feature_worktree(claims_path: Path, feature_id: str) -> str | None:
    claims = _atomic_json_read(claims_path)
    feat = claims.get("features", {}).get(feature_id, {})
    return feat.get("worktree")


def _fail(msg: str):
    _output({"ok": False, "error": msg})
    sys.exit(1)


def _output(data: dict):
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ══════════════════════════════════════════════════════════
#  PLAN.md parsing
# ══════════════════════════════════════════════════════════


def _parse_plan_md(path: Path) -> dict:
    """Parse PLAN.md → {feature_id: {title, task, depends_on, criteria}}."""
    if not path.exists():
        return {}
    text = path.read_text()
    features = {}
    for match in re.finditer(
        r"### Feature (\d+): (.+?)(?=\n### Feature \d+:|\Z)",
        text, re.DOTALL,
    ):
        fid = match.group(1)
        rest = match.group(2)
        title = rest.split("\n")[0].strip()
        # depends_on
        deps_match = re.search(r"\*\*Depends on\*\*: (.+)", rest)
        deps = []
        if deps_match and deps_match.group(1).strip() not in ("—", "无", "none", "None", "-"):
            deps = re.findall(r"Feature (\d+)", deps_match.group(1))
        # task
        task_match = re.search(r"#### Task\n(.+?)(?=\n####|\Z)", rest, re.DOTALL)
        task = task_match.group(1).strip() if task_match else ""
        # criteria
        criteria_match = re.search(
            r"#### Acceptance Criteria\n(.+?)(?=\n####|\n---|\Z)", rest, re.DOTALL
        )
        criteria = criteria_match.group(1).strip() if criteria_match else ""
        features[fid] = {
            "title": title, "task": task,
            "depends_on": deps, "criteria": criteria,
        }
    return features


def _topo_sort(plan: dict) -> list[str]:
    """Topological sort of feature IDs by dependency order."""
    from collections import deque
    in_degree = {fid: 0 for fid in plan}
    adj: dict[str, list[str]] = {fid: [] for fid in plan}
    for fid, spec in plan.items():
        for dep in spec.get("depends_on", []):
            if dep in plan:
                adj[dep].append(fid)
                in_degree[fid] += 1
    queue = deque(fid for fid, d in in_degree.items() if d == 0)
    result = []
    while queue:
        fid = queue.popleft()
        result.append(fid)
        for nxt in adj[fid]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    return result


# ══════════════════════════════════════════════════════════
#  Worktree management
# ══════════════════════════════════════════════════════════


def _create_feature_worktree(
    repo: Path, branch: str, worktree_path: str,
    base_branches: list[str] | None = None,
) -> None:
    """Create a git worktree for a feature, handling dependency base points."""
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    dev_branch = lock.get("branch", "HEAD")

    if not base_branches:
        # No dependencies → branch from dev branch
        _run_git(repo, ["worktree", "add", worktree_path, "-b", branch, dev_branch])
    elif len(base_branches) == 1:
        # Single dependency → branch from that dependency's branch
        _run_git(repo, ["worktree", "add", worktree_path, "-b", branch, base_branches[0]])
    else:
        # Multiple dependencies → branch from dev, merge all deps
        _run_git(repo, ["worktree", "add", worktree_path, "-b", branch, dev_branch])
        for dep_branch in base_branches:
            merge_result = subprocess.run(
                ["git", "merge", dep_branch, "--no-edit"],
                cwd=worktree_path, capture_output=True, text=True,
            )
            if merge_result.returncode != 0:
                # Abort merge, remove worktree, and raise
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=worktree_path, capture_output=True,
                )
                _remove_worktree(repo, worktree_path)
                raise RuntimeError(
                    f"merge conflict merging {dep_branch}: {merge_result.stderr.strip()}"
                )


def _remove_worktree(repo: Path, worktree_path: str) -> None:
    """Remove a git worktree, logging failures but not raising."""
    try:
        result = _run_git(repo, ["worktree", "remove", worktree_path, "--force"], check=False)
        if result.returncode != 0:
            logger.warning("worktree removal failed for %s: %s", worktree_path, result.stderr.strip())
    except Exception as exc:
        logger.warning("worktree removal error for %s: %s", worktree_path, exc)


# ══════════════════════════════════════════════════════════
#  Test execution
# ══════════════════════════════════════════════════════════


def _run_tests(cwd: Path) -> dict:
    """Run tests in the given directory. Returns {ok, output}."""
    from test_runner import TestRunner, _exec, _parse_pytest_output, _resolve_pytest_command

    # Auto-detect test command
    test_cmd = None
    if (cwd / "pyproject.toml").exists():
        test_cmd = _resolve_pytest_command(cwd)
    elif (cwd / "package.json").exists():
        test_cmd = "npm test"
    elif (cwd / "Cargo.toml").exists():
        test_cmd = "cargo test"

    if not test_cmd:
        return {"ok": True, "skipped": True, "output": "no test command detected (skipped)"}

    stdout, stderr, rc = _exec(str(cwd), test_cmd)
    combined = stdout + stderr
    total, passed, failed = _parse_pytest_output(combined)
    output = combined[-TEST_OUTPUT_MAX:] if len(combined) > TEST_OUTPUT_MAX else combined
    summary = f"{passed} passed, {failed} failed" if total > 0 else output[:200]

    return {
        "ok": rc == 0,
        "output": summary if rc == 0 else output,
    }


def _run_lint(cwd: Path) -> dict:
    """Run lint in the given directory. Returns {passed, command, output}."""
    from test_runner import _exec, _has_tool

    lint_cmd = None
    if (cwd / "pyproject.toml").exists():
        if _has_tool(cwd / "pyproject.toml", "ruff"):
            lint_cmd = "ruff check ."
    elif (cwd / "package.json").exists():
        lint_cmd = "npm run lint"
    elif (cwd / "Cargo.toml").exists():
        lint_cmd = "cargo clippy"

    if not lint_cmd:
        return {"passed": True, "skipped": True, "command": None, "output": "no lint command detected (skipped)"}

    stdout, stderr, rc = _exec(str(cwd), lint_cmd)
    combined = stdout + stderr
    output = combined[-TEST_OUTPUT_MAX:] if len(combined) > TEST_OUTPUT_MAX else combined
    return {"passed": rc == 0, "command": lint_cmd, "output": output}


def _run_typecheck(cwd: Path) -> dict:
    """Run typecheck in the given directory. Returns {passed, command, output}."""
    from test_runner import _exec, _has_tool, _resolve_pytest_command

    tc_cmd = _resolve_typecheck_command(cwd)
    if not tc_cmd:
        return {"passed": True, "skipped": True, "command": None, "output": "no typecheck command detected (skipped)"}

    stdout, stderr, rc = _exec(str(cwd), tc_cmd)
    combined = stdout + stderr
    output = combined[-TEST_OUTPUT_MAX:] if len(combined) > TEST_OUTPUT_MAX else combined
    return {"passed": rc == 0, "command": tc_cmd, "output": output}


def _resolve_typecheck_command(cwd: Path) -> str | None:
    """Detect typecheck command for a project."""
    from test_runner import _has_tool

    if (cwd / "pyproject.toml").exists():
        if _has_tool(cwd / "pyproject.toml", "mypy"):
            venv_mypy = cwd / ".venv" / "bin" / "mypy"
            if venv_mypy.is_file():
                return f"{venv_mypy.resolve()} ."
            return "mypy ."
        venv_mypy = cwd / ".venv" / "bin" / "mypy"
        if venv_mypy.is_file():
            return f"{venv_mypy.resolve()} ."
    elif (cwd / "tsconfig.json").exists():
        return "npx tsc --noEmit"
    return None


def _write_evidence(repo: Path, feature_id: str, evidence: dict):
    """Write evidence JSON file for a feature."""
    evidence_dir = repo / CM_DIR / EVIDENCE_DIR
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{feature_id}-verify.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False))


def _delete_evidence(repo: Path, feature_id: str):
    """Delete feature evidence file if it exists."""
    evidence_path = repo / CM_DIR / EVIDENCE_DIR / f"{feature_id}-verify.json"
    try:
        evidence_path.unlink()
    except FileNotFoundError:
        pass


def _read_evidence(repo: Path, feature_id: str) -> dict | None:
    """Read evidence JSON file for a feature. Returns None if not found."""
    evidence_path = repo / CM_DIR / EVIDENCE_DIR / f"{feature_id}-verify.json"
    if not evidence_path.exists():
        return None
    try:
        return json.loads(evidence_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _git_current_branch(path: str | Path) -> str:
    """Get the current git branch name for a path."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(path), capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _delete_integration_report(repo: Path):
    """Delete integration report if it exists."""
    report_path = repo / CM_DIR / EVIDENCE_DIR / "integration-report.json"
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass


# ══════════════════════════════════════════════════════════
#  Precondition checks
# ══════════════════════════════════════════════════════════


def _precondition_check(repo: Path, feature_id: str | None = None) -> dict | None:
    """Check preconditions before mutation commands.

    Returns error dict if precondition violated, None if OK.
    Checks: lease validity, branch consistency, session not done.
    """
    # 1. Lease not expired
    lease = _check_lease(repo)
    if not lease["ok"]:
        return lease

    # 2. Target worktree branch matches claims record
    if feature_id:
        claims = _atomic_json_read(repo / CM_DIR / "claims.json")
        feat = claims.get("features", {}).get(feature_id, {})
        expected_branch = feat.get("branch")
        wt = feat.get("worktree")
        if expected_branch and wt and Path(wt).exists():
            actual_branch = _git_current_branch(wt)
            if actual_branch and actual_branch != expected_branch:
                return {"ok": False, "error": f"Branch mismatch for feature {feature_id}: "
                        f"expected {expected_branch}, worktree on {actual_branch}"}

    # 3. Session not already done
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if lock.get("session_phase") == "done":
        return {"ok": False, "error": "Session already done. Start a new session with cm lock."}

    return None


# ══════════════════════════════════════════════════════════
#  JOURNAL.md
# ══════════════════════════════════════════════════════════


def _append_journal(repo: Path, agent: str, action: str, message: str = ""):
    """flock-protected append-only write to JOURNAL.md."""
    journal_path = repo / CM_DIR / "JOURNAL.md"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    entry = f"\n## {now} [{agent}] {action}\n"
    if message:
        entry += f"{message}\n"
    with open(journal_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ══════════════════════════════════════════════════════════
#  Next-action guidance (injected into cmd_* return values)
# ══════════════════════════════════════════════════════════


def _hint(command: str, reason: str) -> dict:
    """Build a next_action hint dict."""
    return {"command": command, "reason": reason}


# Mode-specific flow maps: after completing step X, suggest step Y.
# Key = (mode, trigger), Value = next_action hint.
_FLOW_AFTER_LOCK = {
    "deliver":  _hint("cm plan-ready", "Validate PLAN.md to unlock feature claiming"),
    "review":   _hint("cm scope --diff HEAD~3..HEAD", "Define what to review"),
    "analyze":  _hint("cm scope --files '...'", "Define what to analyze"),
    "debug":    _hint("cm scope --diff HEAD~3..HEAD", "Define what to investigate"),
}

_FLOW_AFTER_SCOPE = _hint("cm engine-run", "Delegate analysis to engine subprocess")

_FLOW_AFTER_ENGINE = {
    "review":   _hint("cm report --content '...'", "Write review report based on engine findings"),
    "analyze":  _hint("cm report --content '...'", "Write analysis report based on engine findings"),
    "debug":    _hint("cm report --content '...'", "Write diagnosis based on engine findings"),
}

_FLOW_AFTER_REPORT = _hint("cm unlock", "Report written, release session")


# ══════════════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════════════


def cmd_lock(args) -> dict:
    """Lock workspace, create dev branch (or read-only lock for review/analyze).

    Data-layer-first: lock.json is the single source of truth.
    - If an active session exists (session_phase != "done"), join it
      regardless of agent identity or lease expiry.
    - Only create a new session when no lock exists or session is done.
    """
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"

    agent = _resolve_agent(args)
    action_taken = {"type": None}  # track what happened inside atomic update

    raw_mode = getattr(args, "mode", None)
    mode = raw_mode if isinstance(raw_mode, str) else "deliver"
    if mode not in MODES:
        return {"ok": False, "error": f"unknown mode: {mode}", "data": {"available_modes": list(MODES.keys())}}

    read_only = mode in READ_ONLY_MODES

    # Write modes use a separate session worktree, so dirty main repo is fine.
    # No stash or clean-tree check needed — the worktree starts clean.

    # Capture current branch for read-only modes (before lock, no git mutation)
    current_branch = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() if read_only else None

    def reserve_lock(data):
        now = datetime.now(timezone.utc)

        # ── Active session exists (has session_phase, not "done") → join or overlay ──
        if data and data.get("session_phase") and data.get("session_phase") != "done":
            existing_read_only = data.get("read_only", False)

            if read_only and not existing_read_only:
                # Read-only request on a write session: overlay without modifying lock.
                # This prevents review/analyze unlock from destroying a deliver session.
                action_taken["type"] = "overlay"
                return {"ok": True, "data": dict(data)}

            # Same mode family: join the session
            data["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
            agents = data.setdefault("session_agents", [])
            if agent not in agents:
                agents.append(agent)
            action_taken["type"] = "joined"
            return {"ok": True, "data": dict(data), "hint": "session resumed"}

        # ── No session or session done → create new ──
        branch = current_branch if read_only else (
            getattr(args, "branch", None) or f"dev/{args.repo}-{now.strftime('%m%d-%H%M')}"
        )
        data.clear()
        data.update({
            "repo": args.repo,
            "mode": mode,
            "session_phase": "locked",
            "branch": branch,
            "read_only": read_only,
            "locked_by": agent,
            "locked_at": now.isoformat(),
            "lease_expires_at": (now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
            "session_agents": [agent],
        })
        action_taken["type"] = "created"
        return {"ok": True}

    result = _atomic_json_update(lock_path, reserve_lock)
    if not result.get("ok"):
        return result

    next_action = _FLOW_AFTER_LOCK.get(mode)

    # ── Read-only overlay on write session: don't touch lock, just return ──
    if action_taken["type"] == "overlay":
        _append_journal(repo, agent, "lock",
                        f"Read-only overlay ({mode}), branch: {current_branch or '(current)'}")
        return {"ok": True, "data": {"branch": current_branch or "", "read_only": True, "overlay": True,
                                     "next_action": next_action}}

    # ── Joined existing session: validate or recover session worktree ──
    if action_taken["type"] == "joined":
        existing_data = result.get("data", {})
        existing_branch = existing_data.get("branch", "")
        if not existing_data.get("read_only", False):
            session_result = _ensure_session_worktree(repo, existing_data)
            if not session_result.get("ok"):
                return session_result
            existing_data["session_worktree"] = session_result["data"]["session_worktree"]
        _append_journal(repo, agent, "lock", f"Joined session, branch: {existing_branch}")
        existing_data["next_action"] = next_action
        return {"ok": True, "data": existing_data}

    # ── New session created ──
    _ensure_gitignore(repo)

    # Read-only modes: no branch creation, no git state changes
    if read_only:
        _append_journal(repo, agent, "lock", f"Read-only lock ({mode}), branch: {current_branch}")
        return {"ok": True, "data": {"branch": current_branch, "read_only": True,
                                     "next_action": next_action}}

    # Write modes: create session worktree with dev branch (main repo untouched)
    lock = _atomic_json_read(lock_path)
    branch = lock.get("branch", "")
    session_result = _ensure_session_worktree(repo, lock)
    if not session_result.get("ok"):
        _atomic_json_update(lock_path, lambda d: (d.clear(), {"ok": True})[1])
        return {"ok": False, "error": session_result["error"]}
    session_wt = session_result["data"]["session_worktree"]

    _ensure_gitignore(repo)
    _append_journal(repo, agent, "lock", f"Workspace locked, branch: {branch}, worktree: {session_wt}")
    return {"ok": True, "data": {"branch": branch, "session_worktree": session_wt,
                                 "next_action": next_action}}


def cmd_unlock(args) -> dict:
    """Release workspace lock.

    Safety: a write session (deliver/debug) that is not yet done cannot be
    unlocked by a plain `cm unlock`. Only cmd_submit (which sets session_phase
    to "done" first) or `--force` can clear it. This prevents read-only
    overlay sessions from accidentally destroying an in-progress write session.
    """
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"

    lock = _atomic_json_read(lock_path)
    if not lock:
        return {"ok": True}  # already unlocked

    force = getattr(args, "force", False)
    if not force and not lock.get("read_only", False):
        phase = lock.get("session_phase", "")
        if phase and phase != "done":
            return {"ok": False,
                    "error": f"write session in progress (phase={phase}). "
                             "Use cm submit to complete, or cm unlock --force to discard."}

    # Cleanup session worktree (best effort): force unlock or completed session
    session_wt = lock.get("session_worktree", "")
    if session_wt and (force or lock.get("session_phase") == "done"):
        _remove_worktree(repo, session_wt)

    def clear_lock(data):
        data.clear()
        return {"ok": True}

    return _atomic_json_update(lock_path, clear_lock)


def cmd_status(args) -> dict:
    """Show current lock status with exit_status/blocking_reason (read-only)."""
    repo = _repo_path(args.repo)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if not lock:
        return {"ok": True, "data": {"locked": False}}

    data = {
        "locked": True, "expired": _is_expired(lock),
        "branch": lock.get("branch"),
        "session_worktree": lock.get("session_worktree", ""),
        "locked_by": lock.get("locked_by"),
        "session_phase": lock.get("session_phase"),
        "lease_expires_at": lock.get("lease_expires_at"),
        "session_agents": lock.get("session_agents", []),
    }

    # Extended status fields
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    features = claims.get("features", {})

    if plan:
        total = len(plan)
        completed = sum(1 for f in features.values() if f.get("phase") == "done")
        data["features_total"] = total
        data["features_completed"] = completed
        data["evidence_dir"] = str(repo / CM_DIR / EVIDENCE_DIR)

        if lock.get("session_phase") == "done":
            data["exit_status"] = "success"
        elif total > 0 and completed == total:
            data["exit_status"] = "ready"  # all done but not yet submitted
        else:
            data["exit_status"] = "partial"
            # Compute blocking_reason with fixed priority
            blocking_reason, resume_hint = _compute_blocking_reason(repo, lock, plan, features)
            if blocking_reason:
                data["blocking_reason"] = blocking_reason
                data["resume_hint"] = resume_hint

    return {"ok": True, "data": data}


def _compute_blocking_reason(
    repo: Path, lock: dict, plan: dict, features: dict
) -> tuple[str | None, str | None]:
    """Compute blocking reason and resume hint with fixed priority."""
    # Priority 1: expired lease
    if _is_expired(lock):
        return "lease expired", "cm renew or cm unlock"

    # Priority 2: integration failed
    report_path = repo / CM_DIR / EVIDENCE_DIR / "integration-report.json"
    if lock.get("session_phase") == "integrating":
        pass  # integration succeeded, no blocking
    elif report_path.exists():
        try:
            report = json.loads(report_path.read_text())
            if report.get("overall") == "failed":
                ft = report.get("failure_type", "unknown")
                ff = report.get("failed_feature", "?")
                return (f"integration {ft} on feature {ff}",
                        f"cm reopen --feature {ff}, fix, then cm integrate")
        except (json.JSONDecodeError, OSError):
            pass

    # Priority 3: any claimed feature with failed/stale verification
    for fid, feat in features.items():
        if feat.get("phase") == "developing":
            dev = feat.get("developing", {})
            ts = dev.get("test_status", "pending")
            if ts == "failed":
                return (f"feature {fid} verification failed",
                        f"fix and run cm test --feature {fid}")
            if ts == "passed":
                current_head = None
                wt = feat.get("worktree")
                if wt and Path(wt).exists():
                    current_head = _run_git(Path(wt), ["rev-parse", "HEAD"], check=False).stdout.strip()
                else:
                    current_head = dev.get("latest_commit")
                if current_head and dev.get("test_commit") != current_head:
                    return (f"feature {fid} verification stale",
                            f"run cm test --feature {fid}")

    # Priority 4: all remaining features blocked by dependencies
    done_ids = {fid for fid, f in features.items() if f.get("phase") == "done"}
    pending_unblocked = False
    for fid, spec in plan.items():
        if fid in features and features[fid].get("phase") != "pending":
            continue
        deps = spec.get("depends_on", [])
        if not deps or all(d in done_ids for d in deps):
            pending_unblocked = True
            break
    if not pending_unblocked and len(done_ids) < len(plan):
        return "all remaining features blocked by dependencies", "complete in-progress features first"

    return None, None


def cmd_renew(args) -> dict:
    """Renew lease for current lock."""
    repo = _resolve_locked_repo(args)
    lock_path = repo / CM_DIR / "lock.json"

    def do_renew(data):
        if not data:
            return {"ok": False, "error": "no active lock"}
        agent = _resolve_agent(args)
        session_agents = data.get("session_agents", [data.get("locked_by")])
        if agent not in session_agents:
            return {"ok": False, "error": f"agent '{agent}' not in session. "
                    f"Agents: {session_agents}"}
        now = datetime.now(timezone.utc)
        data["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
        data["renewed_by"] = agent
        return {"ok": True, "data": {"new_expires_at": data["lease_expires_at"]}}

    return _atomic_json_update(lock_path, do_renew)


def cmd_plan_ready(args) -> dict:
    """Validate PLAN.md and advance session: locked → reviewed."""
    repo = _resolve_locked_repo(args)
    lock_path = repo / CM_DIR / "lock.json"
    plan_path = repo / CM_DIR / "PLAN.md"

    if not plan_path.exists() or not plan_path.read_text().strip():
        return {"ok": False, "error": "PLAN.md not found or empty"}

    plan = _parse_plan_md(plan_path)
    if not plan:
        return {"ok": False, "error": "PLAN.md contains no parseable features"}

    issues = []
    for fid, spec in plan.items():
        if not spec.get("task", "").strip():
            issues.append(f"Feature {fid}: missing Task section")
        if not spec.get("criteria", "").strip():
            issues.append(f"Feature {fid}: missing Acceptance Criteria")
        for dep in spec.get("depends_on", []):
            if dep not in plan:
                issues.append(f"Feature {fid}: depends on Feature {dep} which does not exist")

    # Check for cycles
    sorted_ids = _topo_sort(plan)
    if len(sorted_ids) != len(plan):
        issues.append("Dependency graph has a cycle")

    if issues:
        return {"ok": False, "error": "PLAN.md validation failed", "data": {"issues": issues}}

    def to_reviewed(data):
        phase = data.get("session_phase")
        if phase == "reviewed":
            return {"ok": True}  # idempotent
        if phase != "locked":
            return {"ok": False, "error": f"session is {phase}, expected locked"}
        data["session_phase"] = "reviewed"
        data["plan_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    result = _atomic_json_update(lock_path, to_reviewed)
    if not result.get("ok"):
        return result

    first_claimable = next((fid for fid in _topo_sort(plan)
                            if not plan[fid].get("depends_on")), list(plan.keys())[0] if plan else "1")
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "plan-ready", f"PLAN.md reviewed: {len(plan)} features")
    return {"ok": True, "data": {
        "features": len(plan), "plan": list(plan.keys()),
        "next_action": _hint(f"cm claim --feature {first_claimable}", "Claim the first feature to start implementing"),
    }}


def cmd_claim(args) -> dict:
    """Claim a feature: create branch/worktree/feature-MD, write claims.json."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    lock_path = repo / CM_DIR / "lock.json"
    feature_id = str(args.feature)
    agent = _resolve_agent(args)

    # Precondition check
    pre_err = _precondition_check(repo)
    if pre_err:
        return pre_err

    # Check session_phase
    lock = _atomic_json_read(lock_path)
    if lock.get("session_phase") == "locked":
        return {"ok": False, "error": "session is locked, "
                "run cm plan-ready first to review PLAN.md before claiming features"}
    if lock.get("session_phase") not in ("reviewed", "working"):
        return {"ok": False, "error": f"session is {lock.get('session_phase')}, cannot claim"}

    # Parse PLAN.md
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    if feature_id not in plan:
        return {"ok": False, "error": f"Feature {feature_id} not found in PLAN.md"}

    # Pre-check (read-only)
    pre_check = _atomic_json_read(claims_path)
    features = pre_check.get("features", {})
    if feature_id in features and features[feature_id].get("phase") not in ("pending", None):
        existing = features[feature_id]
        return {"ok": False, "error": f"already {existing.get('phase')} by {existing.get('agent')}"}
    deps = plan[feature_id].get("depends_on", [])
    for dep in deps:
        dep_phase = features.get(dep, {}).get("phase", "pending")
        if dep_phase != "done":
            return {"ok": False, "error": f"blocked: Feature {dep} is {dep_phase}"}

    branch = f"feat/{feature_id}-{_slugify(plan[feature_id]['title'])}"
    worktree = str(repo.parent / f"{repo.name}-feature-{feature_id}")

    # Create worktree + feature MD (reversible side effects)
    dep_branches = [features[d]["branch"] for d in deps if d in features]
    try:
        _create_feature_worktree(repo, branch, worktree, base_branches=dep_branches)
    except Exception as exc:
        return {"ok": False, "error": f"worktree creation failed: {exc}"}

    spec = plan[feature_id]
    slug = _slugify(spec["title"]) or f"feature-{feature_id}"
    feature_md = repo / CM_DIR / "features" / f"{feature_id.zfill(2)}-{slug}.md"
    _write_file(feature_md, FEATURE_TEMPLATE.format(
        id=feature_id, title=spec["title"],
        task=spec.get("task", ""), criteria=spec.get("criteria", ""),
    ))

    # Atomic commit to claims.json
    def do_claim(data):
        feats = data.setdefault("features", {})
        if feature_id in feats and feats[feature_id].get("phase") not in ("pending", None):
            existing = feats[feature_id]
            return {"ok": False, "error": f"race: already {existing.get('phase')} by {existing.get('agent')}"}
        for dep in deps:
            dep_phase = feats.get(dep, {}).get("phase", "pending")
            if dep_phase != "done":
                return {"ok": False, "error": f"race: dependency Feature {dep} reverted to {dep_phase}"}
        feats[feature_id] = {
            "agent": agent,
            "phase": "analyzing",
            "branch": branch,
            "worktree": worktree,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "analyzing": {"analysis": "pending", "plan": "pending"},
        }
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_claim)
    if not result.get("ok"):
        _remove_worktree(repo, worktree)
        if feature_md.exists():
            feature_md.unlink()
        return result

    # Update session_phase + session_agents
    def update_session(data):
        if data.get("session_phase") == "reviewed":
            data["session_phase"] = "working"
        agents = data.setdefault("session_agents", [])
        if agent not in agents:
            agents.append(agent)
        return {"ok": True}
    _atomic_json_update(lock_path, update_session)

    _append_journal(repo, agent, f"claim feature-{feature_id}",
                    f"Claimed: {spec['title']}")
    return {"ok": True, "data": {
        "feature_md": str(feature_md),
        "branch": branch,
        "worktree": worktree,
        "next_action": _hint(f"cm dev --feature {feature_id}",
                             f"Write Analysis + Plan in {feature_md.name}, then advance to developing"),
    }}


def cmd_dev(args) -> dict:
    """Check Analysis+Plan written → advance feature: analyzing → developing."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # Precondition check
    pre_err = _precondition_check(repo, feature_id)
    if pre_err:
        return pre_err
    delegate_err = _check_delegation_gate(repo, feature_id)
    if delegate_err:
        return delegate_err

    feature_md = _find_feature_md(repo, feature_id)
    has_analysis, has_plan = _check_feature_md_sections(feature_md)

    def do_dev(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        feat = features[feature_id]
        if feat.get("phase") != "analyzing":
            return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected analyzing"}

        analyzing = feat.setdefault("analyzing", {})
        analyzing["analysis"] = "done" if has_analysis else "pending"
        analyzing["plan"] = "done" if has_plan else "pending"

        if not has_analysis:
            return {"ok": False, "error": f"Analysis section is empty in {feature_md}. Write analysis first"}
        if not has_plan:
            return {"ok": False, "error": f"Plan section is empty in {feature_md}. Write plan first"}

        analyzing["completed_at"] = datetime.now(timezone.utc).isoformat()
        feat["phase"] = "developing"
        feat["developing"] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "commit_count": 0,
            "latest_commit": None,
            "test_status": "pending",
            "test_commit": None,
            "test_passed_at": None,
            "test_output": None,
        }
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_dev)
    if result.get("ok"):
        result.setdefault("data", {})["next_action"] = _hint(
            f"cm test --feature {feature_id}",
            "Write code and commit, then run tests")
    return result


def cmd_test(args) -> dict:
    """Run lint+typecheck+tests, write evidence + claims.json."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # Precondition check
    pre_err = _precondition_check(repo, feature_id)
    if pre_err:
        return pre_err
    delegate_err = _check_delegation_gate(repo, feature_id)
    if delegate_err:
        return delegate_err

    worktree = _get_feature_worktree(claims_path, feature_id)
    wt_path = Path(worktree) if worktree else repo

    # Verify feature is still in developing phase before running expensive checks
    pre_claims = _atomic_json_read(claims_path)
    feat = pre_claims.get("features", {}).get(feature_id)
    if not feat:
        return {"ok": False, "error": f"Feature {feature_id} not found"}
    if feat.get("phase") != "developing":
        return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected developing"}

    # Verify no uncommitted changes to tracked files (untracked files are OK)
    git_status = _run_git(wt_path, ["status", "--porcelain", "-uno"], check=False)
    if git_status.stdout.strip():
        return {"ok": False, "error": "uncommitted changes to tracked files, commit before testing"}

    # Get HEAD + commit count
    head = _run_git(wt_path, ["rev-parse", "HEAD"], check=False).stdout.strip()
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    dev_branch = lock.get("branch", "HEAD")
    commit_count = int(
        _run_git(wt_path, ["rev-list", "--count", f"{dev_branch}..HEAD"], check=False)
        .stdout.strip() or "0"
    )

    # Run lint + typecheck + tests
    lint_result = _run_lint(wt_path)
    typecheck_result = _run_typecheck(wt_path)
    test_result = _run_tests(wt_path)

    # Build evidence
    now = datetime.now(timezone.utc).isoformat()
    all_skipped = (lint_result.get("skipped") and typecheck_result.get("skipped")
                   and test_result.get("skipped"))
    if all_skipped:
        overall = "skipped"
    elif lint_result["passed"] and typecheck_result["passed"] and test_result["ok"]:
        overall = "passed"
    else:
        overall = "failed"
    evidence = {
        "feature_id": feature_id,
        "created_at": now,
        "commit": head,
        "lint": {
            "passed": lint_result["passed"],
            "skipped": lint_result.get("skipped", False),
            "command": lint_result.get("command"),
            "output": (lint_result.get("output", "") or "")[:TEST_OUTPUT_MAX],
        },
        "typecheck": {
            "passed": typecheck_result["passed"],
            "skipped": typecheck_result.get("skipped", False),
            "command": typecheck_result.get("command"),
            "output": (typecheck_result.get("output", "") or "")[:TEST_OUTPUT_MAX],
        },
        "test": {
            "passed": test_result["ok"],
            "skipped": test_result.get("skipped", False),
            "command": None,  # auto-detected
            "output": (test_result.get("output", "") or "")[:TEST_OUTPUT_MAX],
        },
        "overall": overall,
    }
    output_summary = (test_result.get("output", "") or "")[:TEST_OUTPUT_MAX]

    def update_test_state(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        feat = features[feature_id]
        if feat.get("phase") != "developing":
            return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected developing"}
        dev = feat.setdefault("developing", {})
        dev["commit_count"] = commit_count
        dev["latest_commit"] = head
        dev["test_commit"] = head
        dev["test_output"] = output_summary
        if overall == "passed":
            dev["test_status"] = "passed"
            dev["test_passed_at"] = now
        else:
            dev["test_status"] = "failed"
            dev["test_passed_at"] = None
        return {"ok": True, "data": {
            "test_passed": overall == "passed",
            "test_status": dev["test_status"],
            "test_commit": head,
            "output": output_summary,
            "evidence": evidence,
        }}

    result = _atomic_json_update(claims_path, update_test_state)
    if not result.get("ok"):
        _delete_evidence(repo, feature_id)
        return result

    _write_evidence(repo, feature_id, evidence)
    if overall == "passed":
        result.setdefault("data", {})["next_action"] = _hint(
            f"cm done --feature {feature_id}", "Tests passed, mark feature complete")
    else:
        result.setdefault("data", {})["next_action"] = _hint(
            f"cm test --feature {feature_id}", "Fix failing tests, then re-run")
    return result


def cmd_done(args) -> dict:
    """Check verification evidence → mark feature done. Returns unblocked features."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    agent = _resolve_agent(args)

    # Precondition check
    pre_err = _precondition_check(repo, feature_id)
    if pre_err:
        return pre_err
    delegate_err = _check_delegation_gate(repo, feature_id)
    if delegate_err:
        return delegate_err

    # Read actual git HEAD (outside flock to avoid blocking)
    worktree = _get_feature_worktree(claims_path, feature_id)
    wt_path = Path(worktree) if worktree else repo
    current_head = _run_git(wt_path, ["rev-parse", "HEAD"], check=False).stdout.strip()

    # Check evidence file (v4 path) or fallback to legacy claims check
    evidence = _read_evidence(repo, feature_id)

    def do_done(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}

        feat = features[feature_id]
        phase = feat.get("phase", "pending")

        if phase == "done":
            return {"ok": False, "error": f"Feature {feature_id} is already done"}
        if phase == "analyzing":
            return {"ok": False, "error": "still in analysis phase, run cm dev first"}
        if phase != "developing":
            return {"ok": False, "error": f"Feature {feature_id} is {phase}, expected developing"}

        if evidence:
            # v4 evidence-based verification
            if evidence.get("commit") != current_head:
                return {"ok": False, "error": "Evidence is stale (code changed after test). Re-run cm test."}
            if evidence.get("overall") == "skipped":
                return {"ok": False, "error": "All verification steps were skipped (no lint/typecheck/test configured). "
                        "Add at least one verification command or write tests before marking done."}
            if evidence.get("overall") != "passed":
                failed = [k for k in ("lint", "typecheck", "test")
                          if not evidence.get(k, {}).get("passed", True)]
                return {"ok": False, "error": f"Verification failed: {', '.join(failed)}. Fix and re-run cm test."}
        else:
            # Legacy fallback for pre-v4 features/sessions
            dev = feat.get("developing", {})
            test_status = dev.get("test_status", "pending")
            if test_status == "pending":
                return {"ok": False, "error": "no test record, run cm test first"}
            if test_status == "failed":
                return {"ok": False, "error": f"last test failed: {dev.get('test_output', '')[:100]}. "
                        "Fix and run cm test again"}
            if dev.get("test_commit") != current_head:
                return {"ok": False, "error": f"code changed after last test "
                        f"(tested {dev.get('test_commit', '?')[:7]}, HEAD {current_head[:7]}), "
                        "run cm test again"}

        feat["phase"] = "done"
        feat["completed_at"] = datetime.now(timezone.utc).isoformat()

        # Find unblocked features
        done_ids = {fid for fid, f in features.items() if f.get("phase") == "done"}
        unblocked = []
        for fid, spec in plan.items():
            if fid in features and features[fid].get("phase") != "pending":
                continue
            fdeps = spec.get("depends_on", [])
            if fdeps and all(d in done_ids for d in fdeps):
                unblocked.append({"id": fid, "title": spec["title"]})

        all_done = all(f.get("phase") == "done" for f in features.values())
        return {"ok": True, "data": {"unblocked": unblocked, "all_done": all_done}}

    result = _atomic_json_update(claims_path, do_done)
    if result.get("ok"):
        _append_journal(repo, agent, f"done feature-{feature_id}")
        data = result.get("data", {})
        if data.get("all_done"):
            data["next_action"] = _hint("cm integrate", "All features done, merge and run integration tests")
        elif data.get("unblocked"):
            next_fid = data["unblocked"][0]["id"]
            data["next_action"] = _hint(f"cm claim --feature {next_fid}",
                                        f"Feature {next_fid} is now unblocked")
        else:
            data["next_action"] = _hint("cm progress", "Check which features are available next")
    return result


def cmd_reopen(args) -> dict:
    """Reopen a done feature back to developing (for integration fix)."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # Precondition check (skip feature-level branch check since it's done, not developing)
    pre_err = _precondition_check(repo)
    if pre_err:
        return pre_err

    def do_reopen(data):
        features = data.setdefault("features", {})
        if feature_id not in features:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        feat = features[feature_id]
        if feat.get("phase") != "done":
            return {"ok": False, "error": f"Feature {feature_id} is {feat.get('phase')}, expected done"}
        feat["phase"] = "developing"
        feat.pop("completed_at", None)
        dev = feat.setdefault("developing", {})
        dev["test_status"] = "pending"
        dev["test_commit"] = None
        dev["test_passed_at"] = None
        dev["test_output"] = None
        dev["reopened_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_reopen)
    if not result.get("ok"):
        return result

    # Session phase back to working
    lock_path = repo / CM_DIR / "lock.json"
    _atomic_json_update(lock_path, lambda d: (
        d.update({"session_phase": "working"}) if d.get("session_phase") == "integrating" else None,
        {"ok": True},
    )[1])

    worktree = _get_feature_worktree(claims_path, feature_id)
    response = {"ok": True, "data": {
        "worktree": worktree, "feature": feature_id, "phase": "developing",
        "next_action": _hint(f"cm test --feature {feature_id}", "Fix the issue, then re-run tests"),
    }}

    # Extract failure context from integration report if available
    report_path = repo / CM_DIR / EVIDENCE_DIR / "integration-report.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
            failure_context = _extract_failure_context(report, feature_id)
            if failure_context:
                response["data"]["failure_context"] = failure_context
        except (json.JSONDecodeError, OSError):
            pass

    # Clear stale integration failure context once recovery starts
    _delete_integration_report(repo)

    return response


def _extract_failure_context(report: dict, feature_id: str) -> dict | None:
    """Extract failure context for a feature from integration report."""
    if report.get("overall") == "passed":
        return None

    failure_type = report.get("failure_type")
    if failure_type == "merge_conflict":
        failed_feature = report.get("failed_feature")
        if failed_feature == feature_id:
            return {
                "type": "merge_conflict",
                "error": report.get("error", ""),
                "conflicting_with": report.get("failed_branch", ""),
            }
        # Feature wasn't the one that failed merge, but was reopened anyway
        return {
            "type": "merge_conflict",
            "error": f"Merge conflict on feature {failed_feature}: {report.get('error', '')}",
        }
    elif failure_type == "test_failure":
        test_info = report.get("test", {})
        return {
            "type": "test_failure",
            "error": (test_info.get("output", "") or "")[:500],
        }
    return None


def cmd_integrate(args) -> dict:
    """Merge all feature branches → run full tests → session: integrating."""
    repo = _resolve_locked_repo(args)

    # Precondition check
    pre_err = _precondition_check(repo)
    if pre_err:
        return pre_err

    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")

    # Check all features done
    for fid in plan:
        phase = claims.get("features", {}).get(fid, {}).get("phase", "pending")
        if phase != "done":
            return {"ok": False, "error": f"Feature {fid} is {phase}, not done. "
                    "All features must be done before integration"}

    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    branch = lock.get("branch", "dev/unknown")

    # All merge/test operations happen in the session worktree (main repo untouched)
    session_wt = _get_session_worktree(repo, lock)
    if not session_wt:
        return {"ok": False, "error": "session_worktree not found. Run cm doctor --fix"}
    wt = session_wt

    pre_merge_sha = _run_git(wt, ["rev-parse", "HEAD"]).stdout.strip()

    # Build merge order and track results
    merge_order = _topo_sort(plan)
    merge_results = []

    for fid in merge_order:
        fb = claims["features"].get(fid, {}).get("branch")
        if not fb:
            continue
        merge_rc = subprocess.run(
            ["git", "merge", fb, "--no-edit"],
            cwd=wt, capture_output=True, text=True,
        )
        if merge_rc.returncode != 0:
            merge_results.append({"feature": fid, "branch": fb, "status": "conflict",
                                  "error": merge_rc.stderr.strip()})
            subprocess.run(["git", "merge", "--abort"], cwd=wt, capture_output=True)
            subprocess.run(
                ["git", "reset", "--hard", pre_merge_sha],
                cwd=wt, capture_output=True,
            )
            # Write integration report (failure)
            report = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "dev_branch": branch,
                "merge_order": merge_order,
                "merge_results": merge_results,
                "overall": "failed",
                "failure_type": "merge_conflict",
                "failed_feature": fid,
                "failed_branch": fb,
                "error": merge_rc.stderr.strip(),
            }
            _write_integration_report(repo, report)
            return {"ok": False, "error": f"merge failed ({fb}): {merge_rc.stderr.strip()}. "
                    "Run cm reopen for the conflicting feature, resolve, then retry"}
        else:
            commit = _run_git(wt, ["rev-parse", "HEAD"], check=False).stdout.strip()
            merge_results.append({"feature": fid, "branch": fb, "status": "merged", "commit": commit})

    # Run full tests on merged dev branch (in session worktree)
    test_result = _run_tests(wt)
    output_summary = (test_result.get("output", "") or "")[:1000]

    if not test_result["ok"]:
        subprocess.run(
            ["git", "reset", "--hard", pre_merge_sha],
            cwd=wt, capture_output=True,
        )
        # Write integration report (test failure)
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dev_branch": branch,
            "merge_order": merge_order,
            "merge_results": merge_results,
            "overall": "failed",
            "failure_type": "test_failure",
            "all_merged": True,
            "test": {"passed": False, "output": output_summary},
        }
        _write_integration_report(repo, report)
        return {"ok": False, "error": "integration tests failed",
                "data": {"output": output_summary,
                         "hint": "cm reopen → fix → cm test → cm done → retry cm integrate"}}

    # Write integration report (success)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dev_branch": branch,
        "merge_order": merge_order,
        "merge_results": merge_results,
        "test": {"passed": True, "output": output_summary},
        "overall": "passed",
    }
    _write_integration_report(repo, report)

    # Success → session_phase = integrating
    _atomic_json_update(repo / CM_DIR / "lock.json", lambda d: (
        d.update({"session_phase": "integrating",
                  "integration_passed_at": datetime.now(timezone.utc).isoformat()}),
        {"ok": True},
    )[1])

    agent = _resolve_agent(args)
    _append_journal(repo, agent, "integrate", "All features merged, integration tests passed")
    return {"ok": True, "data": {
        "test_output": output_summary,
        "next_action": _hint("cm submit --title '...'", "Integration passed, push and create PR"),
    }}


def _write_integration_report(repo: Path, report: dict):
    """Write integration report to evidence directory."""
    evidence_dir = repo / CM_DIR / EVIDENCE_DIR
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / "integration-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))


def cmd_submit(args) -> dict:
    """Idempotent submit: commit → push → PR → cleanup → unlock."""
    repo = _resolve_locked_repo(args)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")

    if lock.get("session_phase") != "integrating":
        return {"ok": False, "error": f"session is {lock.get('session_phase')}, "
                "run cm integrate first"}

    branch = lock.get("branch", "dev/unknown")

    # All git operations in session worktree (main repo untouched)
    session_wt = _get_session_worktree(repo, lock)
    if not session_wt:
        return {"ok": False, "error": "session_worktree not found. Run cm doctor --fix"}
    wt = session_wt

    # Commit (idempotent) — in session worktree
    add_rc = _run_git(wt, ["add", "-A", "--", ":(exclude).coding-master"], check=False)
    if add_rc.returncode != 0:
        return {"ok": False, "error": f"git add failed: {add_rc.stderr.strip()}"}
    status_out = _run_git(wt, ["status", "--porcelain"], check=False).stdout.strip()
    if status_out:
        commit_rc = _run_git(wt, ["commit", "-m", args.title], check=False)
        if commit_rc.returncode != 0:
            return {"ok": False, "error": f"git commit failed: {commit_rc.stderr.strip()}"}

    # Push (idempotent) — from session worktree
    push_rc = _run_git(wt, ["push", "-u", "origin", branch], check=False)
    if push_rc.returncode != 0:
        return {"ok": False, "error": f"git push failed: {push_rc.stderr.strip()}"}

    # PR (idempotent)
    existing_pr = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url"],
        cwd=wt, capture_output=True, text=True,
    )
    pr_url = None
    if existing_pr.returncode != 0:
        pr_body = _generate_pr_body(repo)
        pr_result = subprocess.run(
            ["gh", "pr", "create", "--title", args.title, "--body", pr_body],
            cwd=wt, capture_output=True, text=True,
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
    else:
        try:
            pr_url = json.loads(existing_pr.stdout).get("url")
        except json.JSONDecodeError:
            pass

    # Cleanup feature worktrees + merged feature branches (best effort)
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    for fid in plan:
        feat = claims.get("features", {}).get(fid, {})
        feat_wt = feat.get("worktree")
        if feat_wt:
            _remove_worktree(repo, feat_wt)
        feat_branch = feat.get("branch")
        if feat_branch:
            _run_git(repo, ["branch", "-d", feat_branch], check=False)

    # Cleanup session worktree (best effort)
    _remove_worktree(repo, str(session_wt))

    # Mark done + unlock
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "submit", f"PR: {pr_url or branch}")
    try:
        _atomic_json_update(repo / CM_DIR / "lock.json", lambda d: (
            d.update({"session_phase": "done"}), {"ok": True},
        )[1])
        cmd_unlock(args)
    except Exception as exc:
        return {"ok": True, "data": {"branch": branch, "pr_url": pr_url},
                "warning": f"PR created but unlock failed: {exc}. Run cm doctor to fix."}

    # Build extended contract response
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    features_total = len(plan)
    features_completed = sum(1 for f in claims.get("features", {}).values() if f.get("phase") == "done")
    evidence_dir = str(repo / CM_DIR / EVIDENCE_DIR)

    return {"ok": True, "data": {
        "branch": branch, "pr_url": pr_url,
        "evidence_dir": evidence_dir,
        "features_completed": features_completed,
        "features_total": features_total,
        "exit_status": "success",
        "journal": str(repo / CM_DIR / "JOURNAL.md"),
    }}


def cmd_scope(args) -> dict:
    """Define the scope of analysis/review/debug work. Writes scope.json."""
    repo = _resolve_locked_repo(args)
    mode = getattr(args, "mode_override", None) or _get_session_mode(repo)
    if mode == "deliver":
        return {"ok": False, "error": "scope is not used in deliver mode; use PLAN.md instead"}

    scope = {}
    if getattr(args, "diff", None):
        scope["type"] = "diff"
        scope["diff_range"] = args.diff
    elif getattr(args, "files", None):
        scope["type"] = "files"
        scope["files"] = args.files
    elif getattr(args, "pr", None):
        scope["type"] = "pr"
        scope["pr"] = args.pr
    else:
        scope["type"] = "repo"

    if getattr(args, "goal", None):
        scope["goal"] = args.goal

    scope["created_at"] = datetime.now(timezone.utc).isoformat()
    scope["mode"] = mode

    scope_path = repo / CM_DIR / "scope.json"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(json.dumps(scope, indent=2, ensure_ascii=False))
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "scope", f"Scope defined: {scope.get('type')}")
    return {"ok": True, "data": {"scope_path": str(scope_path), "scope": scope,
                                 "next_action": _FLOW_AFTER_SCOPE}}


def cmd_report(args) -> dict:
    """Write or finalize the session report/diagnosis. Writes report.md or diagnosis.md."""
    repo = _resolve_locked_repo(args)
    mode = getattr(args, "mode_override", None) or _get_session_mode(repo)
    if mode == "deliver":
        return {"ok": False, "error": "report is not used in deliver mode; use cm submit"}

    # Determine output filename based on mode
    if mode == "debug":
        filename = "diagnosis.md"
    else:
        filename = "report.md"

    content = getattr(args, "content", None) or ""
    report_file = getattr(args, "file", None)

    if report_file:
        src = Path(report_file)
        if not src.exists():
            return {"ok": False, "error": f"report file not found: {report_file}"}
        content = src.read_text()

    if not content.strip():
        return {"ok": False, "error": "report content is empty; provide --content or --file"}

    report_path = repo / CM_DIR / filename
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(content)
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "report", f"Report written: {filename}")

    # Auto-unlock for review/analyze/debug — report is the terminal artifact.
    auto_unlocked = False
    if mode in ("review", "analyze", "debug"):
        try:
            cmd_unlock(args)
            auto_unlocked = True
        except Exception:
            pass

    result = {
        "ok": True,
        "data": {
            "report_path": str(report_path),
            "filename": filename,
            "report_content": content,
            "auto_unlocked": auto_unlocked,
            "instruction": (
                "Report saved. Present the report_content to the user "
                "as a well-formatted message. Do NOT output raw JSON."
            ),
        },
    }
    if not auto_unlocked:
        result["data"]["next_action"] = _FLOW_AFTER_REPORT
    return result


def _build_scope_description(scope: dict) -> str:
    """Convert scope.json data into a human-readable description for engine prompts."""
    parts = []
    scope_type = scope.get("type", "repo")
    if scope_type == "diff":
        parts.append(f"Diff range: {scope.get('diff_range', 'unknown')}")
    elif scope_type == "files":
        files = scope.get("files", [])
        parts.append(f"Files: {', '.join(files[:20])}")
        if len(files) > 20:
            parts.append(f"  ... and {len(files) - 20} more files")
    elif scope_type == "pr":
        parts.append(f"PR: {scope.get('pr', 'unknown')}")
    else:
        parts.append("Scope: entire repository")

    if scope.get("goal"):
        parts.append(f"Goal: {scope['goal']}")

    return "\n".join(parts)


def _get_scope_context(repo: Path, scope: dict) -> str:
    """Get diff text or file listing from scope, capped at 20KB for engine prompt."""
    max_bytes = 20_000
    scope_type = scope.get("type", "repo")

    if scope_type == "diff":
        diff_range = scope.get("diff_range", "HEAD~1..HEAD")
        try:
            result = _run_git(repo, ["diff", diff_range], check=False)
            diff_text = result.stdout
            if len(diff_text) > max_bytes:
                diff_text = diff_text[:max_bytes] + "\n...(diff truncated)..."
            return f"## Diff ({diff_range})\n```\n{diff_text}\n```"
        except Exception:
            return f"(failed to get diff for {diff_range})"

    elif scope_type == "files":
        files = scope.get("files", [])
        return f"## Target Files\n{chr(10).join(files[:50])}"

    elif scope_type == "pr":
        pr = scope.get("pr", "")
        try:
            result = subprocess.run(
                ["gh", "pr", "diff", str(pr)],
                cwd=str(repo), capture_output=True, text=True, timeout=30,
            )
            diff_text = result.stdout
            if len(diff_text) > max_bytes:
                diff_text = diff_text[:max_bytes] + "\n...(diff truncated)..."
            return f"## PR #{pr} Diff\n```\n{diff_text}\n```"
        except Exception:
            return f"(failed to get PR diff for {pr})"

    return "(no specific scope context)"


def cmd_engine_run(args) -> dict:
    """Delegate code analysis to an engine subprocess (e.g. Claude Code CLI).

    Reads scope.json for analysis range, builds a mode-specific prompt,
    invokes the engine, and saves the result to engine_result.json.
    """
    from engine import get_engine, MODE_PROMPTS

    repo = _resolve_locked_repo(args)
    mode = _get_session_mode(repo)

    # Read scope
    scope_path = repo / CM_DIR / "scope.json"
    if not scope_path.exists():
        return {"ok": False, "error": "no scope defined. Run cm scope first."}

    scope = _atomic_json_read(scope_path)
    if not scope:
        return {"ok": False, "error": "scope.json is empty. Run cm scope to define scope."}

    # Build prompt
    if mode not in MODE_PROMPTS:
        return {"ok": False, "error": f"no engine prompt template for mode '{mode}'. "
                f"Available: {', '.join(MODE_PROMPTS)}"}
    mode_prompt = MODE_PROMPTS[mode]
    scope_desc = _build_scope_description(scope)
    scope_context = _get_scope_context(repo, scope)

    goal = getattr(args, "goal", None) or scope.get("goal", "")
    goal_section = f"\n## Goal\n{goal}\n" if goal else ""

    prompt = f"{mode_prompt}## Scope\n{scope_desc}\n{goal_section}\n{scope_context}"

    # Get engine
    engine_name = getattr(args, "engine", None) or "claude-code"
    timeout = getattr(args, "timeout", None) or 600
    max_turns = getattr(args, "max_turns", None) or 30

    try:
        engine = get_engine(engine_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not engine.is_available():
        return {"ok": False, "error": f"Engine '{engine_name}' is not available. "
                f"Ensure the CLI is installed and in PATH."}

    # Run engine
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "engine-run",
                    f"Starting {engine_name} engine, mode={mode}, scope={scope.get('type')}")

    result = engine.run(
        prompt=prompt,
        repo_path=repo,
        mode=mode,
        timeout=timeout,
        max_turns=max_turns,
    )

    # Save result
    result_path = repo / CM_DIR / "engine_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))

    _append_journal(repo, agent, "engine-run",
                    f"Engine finished: ok={result.ok}, "
                    f"findings={len(result.findings)}, turns={result.turns_used}")

    data = result.to_dict()
    if result.ok:
        data["next_action"] = _FLOW_AFTER_ENGINE.get(mode, _hint("cm report --content '...'", "Write report based on findings"))
    return {"ok": result.ok, "data": data}


def cmd_delegate_prepare(args) -> dict:
    """Write delegation request and move feature delegation state to running."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    spec = plan.get(feature_id)
    if not spec:
        return {"ok": False, "error": f"Feature {feature_id} not found in PLAN.md"}

    def do_prepare(data):
        features = data.setdefault("features", {})
        feature = features.get(feature_id)
        if not feature:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        delegation = feature.setdefault("delegation", {})
        if not delegation.get("required"):
            delegation.update(
                {
                    "required": True,
                    "task_type": "analyze-implementation",
                    "reason": "explicit_delegate_prepare",
                    "status": "pending",
                }
            )
        if delegation.get("status") == "completed":
            return {"ok": True}
        delegation["status"] = "running"
        delegation["prepared_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_prepare)
    if not result.get("ok"):
        return result

    request = {
        "task_type": "analyze-implementation",
        "repo": repo.name,
        "feature_id": feature_id,
        "targets": [
            {
                "path": str(_find_feature_md(repo, feature_id) or ""),
                "symbol": None,
            }
        ],
        "goal": spec.get("task", "") or spec.get("title", ""),
        "acceptance_criteria": spec.get("criteria", ""),
    }
    req_path = _delegation_request_path(repo, feature_id)
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(json.dumps(request, indent=2, ensure_ascii=False))
    return {
        "ok": True,
        "data": {
            "feature": feature_id,
            "request_path": str(req_path),
            "required_artifacts": [
                str(_delegation_result_path(repo, feature_id)),
                str(_delegation_behavior_summary_path(repo, feature_id)),
                str(_delegation_edge_case_matrix_path(repo, feature_id)),
            ],
        },
    }


def cmd_delegate_complete(args) -> dict:
    """Validate delegation artifacts and unlock execute for the feature."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)
    claims = _atomic_json_read(claims_path)
    feature = claims.get("features", {}).get(feature_id)
    if not feature:
        return {"ok": False, "error": f"Feature {feature_id} not found"}
    delegation = feature.get("delegation")
    if not delegation or not delegation.get("required"):
        return {"ok": False, "error": f"Feature {feature_id} does not require delegation"}

    ready, missing = _delegation_artifacts_ready(repo, feature_id, delegation)
    if not ready:
        return {
            "ok": False,
            "error": "delegation artifacts incomplete",
            "data": {"feature": feature_id, "missing": missing},
        }

    result_payload = {}
    result_path = _delegation_result_path(repo, feature_id)
    try:
        result_payload = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "invalid delegation result.json"}

    def do_complete(data):
        features = data.setdefault("features", {})
        current = features.get(feature_id)
        if not current:
            return {"ok": False, "error": f"Feature {feature_id} not found"}
        current_delegation = current.get("delegation")
        if not current_delegation or not current_delegation.get("required"):
            return {"ok": False, "error": f"Feature {feature_id} does not require delegation"}
        current_delegation["status"] = "completed"
        current_delegation["completed_at"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    result = _atomic_json_update(claims_path, do_complete)
    if not result.get("ok"):
        return result

    return {
        "ok": True,
        "data": {
            "feature": feature_id,
            "delegation_status": "completed",
            "recommended_next_step": result_payload.get("recommended_next_step"),
        },
    }


def cmd_progress(args) -> dict:
    """Read-only: show session + feature status + action guidance."""
    repo = _resolve_locked_repo(args)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    mode = lock.get("mode", "deliver")
    session_phase = lock.get("session_phase", "unknown")

    # Non-deliver modes: artifact-gap based progress
    if mode != "deliver":
        return _progress_artifact_mode(repo, lock, mode, session_phase, args)

    # Deliver mode: feature-based progress (existing logic)
    plan_path = repo / CM_DIR / "PLAN.md"
    plan = _parse_plan_md(plan_path)
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    features_claims = claims.get("features", {})

    plan_exists = plan_path.exists() and bool(plan)

    session_steps = _generate_session_steps(session_phase, plan_exists)

    result = []
    done_ids = {fid for fid, f in features_claims.items() if f.get("phase") == "done"}
    for fid, spec in plan.items():
        claim = features_claims.get(fid, {})
        phase = claim.get("phase", "pending")
        blocked_by = []

        if phase == "pending":
            fdeps = spec.get("depends_on", [])
            blocked_by = [d for d in fdeps if d not in done_ids]
            if blocked_by:
                phase = "blocked"

        feature_md = _find_feature_md(repo, fid) if phase not in ("pending", "blocked") else None
        action_steps = _generate_action_steps(
            phase, claim, fid, str(feature_md) if feature_md else None, blocked_by,
        )

        result.append({
            "id": fid, "title": spec["title"],
            "phase": phase,
            "agent": claim.get("agent"),
            "worktree": claim.get("worktree"),
            "feature_md": str(feature_md) if feature_md else None,
            "delegation": claim.get("delegation"),
            "action_steps": action_steps,
        })

    suggestions = _generate_suggestions(result, lock)

    agent = _resolve_agent(args)
    next_action = _compute_next_action(repo, result, features_claims, lock, agent)
    session_next_action = _compute_session_next_action(repo, result, features_claims, lock)
    delegation_info = _compute_local_delegation(result, features_claims, agent)
    must_delegate = delegation_info is not None

    return {"ok": True, "data": {
        "mode": mode,
        "session_phase": session_phase,
        "session_worktree": lock.get("session_worktree", ""),
        "session_steps": session_steps,
        "must_delegate": must_delegate,
        "delegation": delegation_info,
        "next_action": next_action,
        "session_next_action": session_next_action,
        "total": len(result),
        "done": sum(1 for r in result if r["phase"] == "done"),
        "analyzing": sum(1 for r in result if r["phase"] == "analyzing"),
        "developing": sum(1 for r in result if r["phase"] == "developing"),
        "pending": sum(1 for r in result if r["phase"] == "pending"),
        "blocked": sum(1 for r in result if r["phase"] == "blocked"),
        "features": result,
        "suggestions": suggestions,
    }}


def _progress_artifact_mode(repo: Path, lock: dict, mode: str, session_phase: str, args) -> dict:
    """Progress for non-deliver modes: report artifact gaps and suggest next steps."""
    mode_def = MODES.get(mode, MODES["deliver"])
    artifact_status = _get_mode_artifact_status(repo)
    completion_ready, missing = _check_mode_completion(repo)

    # Read scope if it exists
    scope_path = repo / CM_DIR / "scope.json"
    scope = {}
    if scope_path.exists():
        try:
            scope = json.loads(scope_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Compute suggestions based on what's missing
    suggestions = []
    if "scope.json" in artifact_status and artifact_status["scope.json"] == "missing":
        suggestions.append("Define scope: cm scope --diff HEAD~3..HEAD or cm scope --files 'src/*.py'")
    if completion_ready:
        suggestions.append("All artifacts ready. Run cm unlock to finish.")
    else:
        for name in missing:
            if name == "report.md":
                suggestions.append("Write report: cm report --content '...' or cm report --file report.md")
            elif name == "diagnosis.md":
                suggestions.append("Write diagnosis: cm report --content '...' or cm report --file diagnosis.md")

    # Next action
    next_action = None
    if not scope and "scope.json" in {a for a in mode_def["required_artifacts"]}:
        next_action = {"command": "cm scope", "reason": "Define what to analyze", "scope": "local"}
    elif completion_ready:
        next_action = {"command": "cm unlock", "reason": "Work complete, release session", "scope": "local"}
    elif "scope.json" in artifact_status and artifact_status["scope.json"] == "exists":
        if mode == "debug":
            next_action = {"command": None, "reason": "Investigate and write diagnosis", "scope": "local"}
        else:
            next_action = {"command": None, "reason": "Analyze scope and write report", "scope": "local"}

    return {"ok": True, "data": {
        "mode": mode,
        "mode_description": mode_def["description"],
        "session_phase": session_phase,
        "artifact_status": artifact_status,
        "completion_ready": completion_ready,
        "missing_artifacts": missing,
        "scope": scope,
        "next_action": next_action,
        "suggestions": suggestions,
    }}


def _compute_next_action(
    repo: Path, features: list[dict], claims: dict, lock: dict, agent: str,
) -> dict | None:
    """Compute the best next action for the current agent (local scope)."""
    session_phase = lock.get("session_phase", "unknown")

    # Session-level actions that any agent can do
    if session_phase == "integrating":
        return {"command": "cm submit --title '...'", "reason": "Integration passed, ready to submit", "scope": "local"}

    # Check agent's own features first
    for f in features:
        fid = f["id"]
        claim = claims.get(fid, {})
        if claim.get("agent") != agent:
            continue
        delegation = claim.get("delegation", {})
        if delegation.get("required") and delegation.get("status") != "completed":
            if delegation.get("status") == "pending":
                return {
                    "command": f"cm delegate-prepare --feature {fid}",
                    "reason": delegation.get("reason", "delegation required"),
                    "worktree": claim.get("worktree"),
                    "scope": "local",
                }
            ready, missing = _delegation_artifacts_ready(repo, fid, delegation)
            if ready:
                return {
                    "command": f"cm delegate-complete --feature {fid}",
                    "reason": "Delegation artifacts are ready, unlock execute",
                    "worktree": claim.get("worktree"),
                    "scope": "local",
                }
            return None
        dev = claim.get("developing", {})

        if f["phase"] == "developing":
            ts = dev.get("test_status", "pending")
            if ts == "failed":
                output = (dev.get("test_output", "") or "")[:100]
                return {"command": f"fix and cm test --feature {fid}",
                        "reason": f"Test failed: {output}", "worktree": claim.get("worktree"), "scope": "local"}
            if ts == "passed" and dev.get("test_commit") != dev.get("latest_commit"):
                return {"command": f"cm test --feature {fid}",
                        "reason": "Code changed after test (stale)", "worktree": claim.get("worktree"), "scope": "local"}
            if ts == "passed" and dev.get("test_commit") == dev.get("latest_commit"):
                return {"command": f"cm done --feature {fid}",
                        "reason": "Verification passed, ready to mark done", "worktree": claim.get("worktree"), "scope": "local"}
            if ts == "pending":
                return {"command": f"Write code, commit, cm test --feature {fid}",
                        "reason": "Feature in development, no tests yet", "worktree": claim.get("worktree"), "scope": "local"}

        if f["phase"] == "analyzing":
            return {"command": f"Write Analysis+Plan, cm dev --feature {fid}",
                    "reason": "Feature needs analysis and planning", "worktree": claim.get("worktree"), "scope": "local"}

    # Agent has no in-progress features — look for unclaimed/unblocked
    for f in features:
        if f["phase"] == "pending":
            return {"command": f"cm claim --feature {f['id']}",
                    "reason": f"Feature {f['id']} ({f['title']}) is available", "scope": "local"}

    # All features done, suggest integrate
    if all(f["phase"] == "done" for f in features) and session_phase == "working":
        return {"command": "cm integrate", "reason": "All features done, ready to integrate", "scope": "local"}

    return None


def _compute_session_next_action(
    repo: Path, features: list[dict], claims: dict, lock: dict,
) -> dict | None:
    """Compute the best next action for the session (global scope)."""
    session_phase = lock.get("session_phase", "unknown")

    if session_phase == "integrating":
        return {"command": "cm submit --title '...'", "reason": "Integration passed, ready to submit", "scope": "session"}

    # Any feature with failed/stale verification
    for f in features:
        fid = f["id"]
        claim = claims.get(fid, {})
        delegation = claim.get("delegation", {})
        if delegation.get("required") and delegation.get("status") != "completed":
            if delegation.get("status") == "pending":
                return {
                    "command": f"cm delegate-prepare --feature {fid}",
                    "reason": f"Feature {fid} requires delegation before execute",
                    "scope": "session",
                }
            ready, missing = _delegation_artifacts_ready(repo, fid, delegation)
            if ready:
                return {
                    "command": f"cm delegate-complete --feature {fid}",
                    "reason": f"Feature {fid} delegation artifacts ready, unlock execute",
                    "scope": "session",
                }
            continue
        dev = claim.get("developing", {})
        owner = claim.get("agent", "unknown")

        if f["phase"] == "developing":
            ts = dev.get("test_status", "pending")
            if ts == "failed":
                return {"command": f"cm test --feature {fid}",
                        "reason": f"Feature {fid} verification failed (owner: {owner})", "scope": "session"}
            if ts == "passed" and dev.get("test_commit") != dev.get("latest_commit"):
                return {"command": f"cm test --feature {fid}",
                        "reason": f"Feature {fid} test stale (owner: {owner})", "scope": "session"}

    # Any feature ready to mark done
    for f in features:
        fid = f["id"]
        claim = claims.get(fid, {})
        dev = claim.get("developing", {})
        owner = claim.get("agent", "unknown")
        if f["phase"] == "developing":
            ts = dev.get("test_status", "pending")
            if ts == "passed" and dev.get("test_commit") == dev.get("latest_commit"):
                return {"command": f"cm done --feature {fid}",
                        "reason": f"Feature {fid} verified, ready to mark done (owner: {owner})", "scope": "session"}

    # Any analyzing feature
    for f in features:
        if f["phase"] == "analyzing":
            owner = claims.get(f["id"], {}).get("agent", "unknown")
            return {"command": f"cm dev --feature {f['id']}",
                    "reason": f"Feature {f['id']} needs analysis (owner: {owner})", "scope": "session"}

    # Unclaimed/unblocked features
    for f in features:
        if f["phase"] == "pending":
            return {"command": f"cm claim --feature {f['id']}",
                    "reason": f"Feature {f['id']} ({f['title']}) available for claiming", "scope": "session"}

    # All done
    if all(f["phase"] == "done" for f in features) and session_phase == "working":
        return {"command": "cm integrate", "reason": "All features done, ready to integrate", "scope": "session"}

    return None


def _compute_local_delegation(features: list[dict], claims: dict, agent: str) -> dict | None:
    """Return delegation summary for the current agent if delegation is required."""
    for f in features:
        fid = f["id"]
        claim = claims.get(fid, {})
        if claim.get("agent") != agent:
            continue
        delegation = claim.get("delegation", {})
        if delegation.get("required") and delegation.get("status") != "completed":
            return {
                "feature": fid,
                "task_type": delegation.get("task_type"),
                "reason": delegation.get("reason"),
                "status": delegation.get("status"),
            }
    return None


def _generate_session_steps(session_phase: str, plan_exists: bool) -> list[str]:
    if session_phase == "locked":
        if plan_exists:
            return ["Check PLAN.md completeness", "Run cm plan-ready"]
        return ["Analyze requirements", "Create .coding-master/PLAN.md"]
    if session_phase == "reviewed":
        return ["Run cm claim --feature N"]
    if session_phase == "working":
        return []
    if session_phase == "integrating":
        return ["Run cm submit --title '...'"]
    return []


def _generate_action_steps(
    phase: str, claim: dict, fid: str,
    feature_md: str | None, blocked_by: list[str],
) -> list[str]:
    wt = claim.get("worktree", "")
    if phase == "blocked":
        return [f"Waiting: {', '.join(f'Feature {d}' for d in blocked_by)}"]
    if phase == "pending":
        return [f"cm claim --feature {fid}"]
    if phase == "analyzing":
        a = claim.get("analyzing", {})
        if a.get("analysis") != "done":
            return [f"cd {wt}", f"Read {feature_md}", "Write Analysis section"]
        if a.get("plan") != "done":
            return [f"Write Plan in {feature_md}", f"cm dev --feature {fid}"]
        return [f"cm dev --feature {fid}"]
    if phase == "developing":
        dev = claim.get("developing", {})
        ts = dev.get("test_status", "pending")
        if ts == "pending":
            return [f"cd {wt}", "Write code", "git commit", f"cm test --feature {fid}"]
        if ts == "failed":
            output = dev.get("test_output", "")[:200]
            return [f"cd {wt}", f"Fix: {output}", "git commit", f"cm test --feature {fid}"]
        if ts == "passed":
            if dev.get("test_commit") != dev.get("latest_commit"):
                return [f"cd {wt}", f"cm test --feature {fid} (code changed after test)"]
            return [f"Check AC in {feature_md}", f"cm done --feature {fid}"]
    return ["Done"]


def _generate_suggestions(features: list[dict], lock: dict) -> list[str]:
    suggestions = []
    session_phase = lock.get("session_phase", "unknown")
    all_done = all(f["phase"] == "done" for f in features) if features else False

    if session_phase == "integrating":
        suggestions.append("Run cm submit")
        return suggestions
    if all_done and session_phase == "working":
        suggestions.append("All features done, run cm integrate")
        return suggestions

    for f in features:
        if f["phase"] == "pending":
            suggestions.append(f"Feature {f['id']} ({f['title']}) available to claim")
    return suggestions


def cmd_journal(args) -> dict:
    """Append a message to JOURNAL.md."""
    repo = _resolve_locked_repo(args)
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "note", args.message)
    return {"ok": True}


def cmd_repos(_args) -> dict:
    """List configured repos and workspaces."""
    cfg = ConfigManager()
    return cfg.list_all()


def cmd_doctor(args) -> dict:
    """Diagnose and fix state inconsistencies."""
    repo = _repo_path(args.repo)
    issues = []
    fixes = []

    # 1. Lock state
    lock_path = repo / CM_DIR / "lock.json"
    if lock_path.exists():
        lock = _atomic_json_read(lock_path)
        if lock:
            if _is_expired(lock):
                issues.append(f"lock expired at {lock.get('lease_expires_at')}")
                fixes.append("cm unlock or cm renew")
            branch = lock.get("branch", "")
            if branch:
                branch_check = _run_git(repo, ["rev-parse", "--verify", branch], check=False)
                if branch_check.returncode != 0:
                    issues.append(f"lock references branch '{branch}' which does not exist")
                    fixes.append("cm unlock --force")
            # Check session_worktree health for write sessions
            if not lock.get("read_only", False):
                session_wt = lock.get("session_worktree", "")
                if not session_wt:
                    issues.append("session_worktree missing from lock")
                    fixes.append("cm doctor --fix (will recreate session worktree)")
                elif not Path(session_wt).exists():
                    issues.append(f"session_worktree '{session_wt}' does not exist")
                    fixes.append("cm doctor --fix (will recreate session worktree)")

    # 2. Claims worktree existence
    claims_path = repo / CM_DIR / "claims.json"
    if claims_path.exists():
        claims = _atomic_json_read(claims_path)
        for fid, feat in claims.get("features", {}).items():
            if feat.get("phase") in ("analyzing", "developing"):
                wt = feat.get("worktree", "")
                if wt and not Path(wt).exists():
                    issues.append(f"Feature {fid}: worktree '{wt}' does not exist")
                    fixes.append(f"cm doctor --fix (will reset Feature {fid} to pending)")

    # 3. Orphaned worktrees (feature + session)
    expected_worktrees = set()
    # Session worktree from lock.json
    if lock_path.exists():
        _lock = _atomic_json_read(lock_path)
        if _lock.get("session_worktree"):
            expected_worktrees.add(_lock["session_worktree"])
    # Feature worktrees from claims.json
    if claims_path.exists():
        for feat in _atomic_json_read(claims_path).get("features", {}).values():
            if feat.get("worktree"):
                expected_worktrees.add(feat["worktree"])
    for d in repo.parent.iterdir():
        is_feature_wt = d.name.startswith(f"{repo.name}-feature-")
        is_session_wt = d.name == f"{repo.name}-session"
        if (is_feature_wt or is_session_wt) and str(d) not in expected_worktrees:
            issues.append(f"orphaned worktree: {d}")
            fixes.append(f"cm doctor --fix (will remove {d})")

    # 4. PLAN.md vs claims.json consistency
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    if claims_path.exists():
        claims = _atomic_json_read(claims_path)
        for fid in claims.get("features", {}):
            if fid not in plan:
                issues.append(f"claims.json references Feature {fid} not in PLAN.md")

    # 5. Orphaned branches (dev/* and feat/* with no active session)
    lock = _atomic_json_read(lock_path) if lock_path.exists() else {}
    active_branch = lock.get("branch", "") if lock else ""
    active_feat_branches = set()
    if claims_path.exists():
        for feat in _atomic_json_read(claims_path).get("features", {}).values():
            if feat.get("branch"):
                active_feat_branches.add(feat["branch"])

    branch_output = _run_git(repo, ["branch", "--list", "dev/*", "feat/*"], check=False).stdout
    for line in branch_output.splitlines():
        branch_name = line.strip().lstrip("* ")
        if not branch_name:
            continue
        if branch_name == active_branch or branch_name in active_feat_branches:
            continue
        # Check if merged into main
        merged = _run_git(repo, ["branch", "--merged", "main", "--list", branch_name], check=False)
        if merged.stdout.strip():
            issues.append(f"orphaned branch (merged): {branch_name}")
            fixes.append(f"cm doctor --fix (will delete {branch_name})")
        elif not lock:
            # No active session at all → all dev/feat branches are orphaned
            issues.append(f"orphaned branch (no session): {branch_name}")
            fixes.append(f"cm doctor --fix (will delete {branch_name})")

    # Auto-fix
    if getattr(args, "fix", False) and issues:
        _doctor_auto_fix(repo, issues)
        fixes = [f"auto-fixed: {f}" for f in fixes]

    return {"ok": len(issues) == 0, "data": {"issues": issues, "suggested_fixes": fixes}}


def _doctor_auto_fix(repo: Path, issues: list[str]):
    """Best-effort auto-fix for detected issues."""
    claims_path = repo / CM_DIR / "claims.json"

    for issue in issues:
        if "orphaned worktree" in issue:
            wt_path = issue.split(": ", 1)[1] if ": " in issue else ""
            if wt_path and Path(wt_path).exists():
                _remove_worktree(repo, wt_path)
                # Fallback: if git worktree remove didn't work (e.g. not a real worktree)
                import shutil
                if Path(wt_path).exists():
                    shutil.rmtree(wt_path, ignore_errors=True)

        elif "worktree" in issue and "does not exist" in issue:
            if issue.startswith("session_worktree"):
                session_result = _ensure_session_worktree(repo)
                if not session_result.get("ok"):
                    continue
                continue
            # Reset feature to pending
            fid_match = re.search(r"Feature (\d+)", issue)
            if fid_match:
                fid = fid_match.group(1)

                def reset_feature(data, _fid=fid):
                    feats = data.get("features", {})
                    if _fid in feats:
                        feats[_fid] = {"phase": "pending"}
                    return {"ok": True}
                _atomic_json_update(claims_path, reset_feature)

        elif issue == "session_worktree missing from lock":
            session_result = _ensure_session_worktree(repo)
            if not session_result.get("ok"):
                continue

        elif "expired" in issue:
            pass  # Don't auto-fix expired locks — user should decide

        elif "orphaned branch" in issue:
            branch_name = issue.split(": ", 1)[1] if ": " in issue else ""
            if branch_name:
                _run_git(repo, ["branch", "-D", branch_name], check=False)


def _generate_pr_body(repo: Path) -> str:
    """Generate PR body from JOURNAL.md milestones + PLAN.md features."""
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    journal_path = repo / CM_DIR / "JOURNAL.md"

    lines = ["## Features\n"]
    for fid in _topo_sort(plan):
        lines.append(f"- **Feature {fid}**: {plan[fid]['title']}")
    lines.append("")

    if journal_path.exists():
        lines.append("## Timeline\n")
        for line in journal_path.read_text().splitlines():
            if re.match(
                r"^## \d{4}-\d{2}-\d{2}T\d{2}:\d{2} \[.*?\] (done|submit|plan-ready|integrate)",
                line,
            ):
                lines.append(line)
        lines.append("")

    return "\n".join(lines)


def cmd_start(args) -> dict:
    """One-shot session setup: lock + copy plan + plan-ready. Rolls back on failure."""
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"
    plan_path = repo / CM_DIR / "PLAN.md"

    # Step 1: Lock (cmd_lock handles join-or-create)
    lock_result = cmd_lock(args)
    if not lock_result.get("ok"):
        return lock_result

    plan_created = False
    try:
        # Step 2: Copy plan file
        plan_file = getattr(args, "plan_file", None)
        if plan_file:
            src = Path(plan_file)
            if not src.exists():
                raise RuntimeError(f"plan file not found: {plan_file}")
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(src.read_text())
            plan_created = True

        # Step 3: Plan-ready (only if plan exists)
        if plan_path.exists() and plan_path.read_text().strip():
            ready_result = cmd_plan_ready(args)
            if not ready_result.get("ok"):
                raise RuntimeError(ready_result.get("error", "plan-ready failed"))
            return {"ok": True, "data": {
                "branch": lock_result["data"]["branch"],
                "session_worktree": lock_result["data"].get("session_worktree", ""),
                "plan": ready_result.get("data", {}),
                "rolled_back": False,
            }}
        else:
            # No plan yet — return locked state, user will create plan
            return {"ok": True, "data": {
                "branch": lock_result["data"]["branch"],
                "session_worktree": lock_result["data"].get("session_worktree", ""),
                "session_phase": "locked",
                "rolled_back": False,
            }}

    except Exception as exc:
        # Best-effort rollback: force-unlock since we just created this session
        if plan_created and plan_path.exists():
            try:
                plan_path.unlink()
            except OSError:
                pass
        force_args = copy.copy(args)
        force_args.force = True
        cmd_unlock(force_args)
        return {"ok": False, "error": str(exc), "data": {"rolled_back": True}}


# ══════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════


def _add_global_args(parser: argparse.ArgumentParser, *, is_parent: bool = False) -> None:
    """Register --repo and --agent on *parser*.

    On the parent parser, use ``default=None`` so the value is always present.
    On sub-parsers, use ``default=SUPPRESS`` so the sub-parser only sets the
    attribute when the user explicitly passes the flag — otherwise the parent's
    value is preserved.
    """
    default = None if is_parent else argparse.SUPPRESS
    parser.add_argument("--repo", "-r", default=default, help="Target repo name")
    parser.add_argument("--agent", default=default, help="Agent identity")


def main():
    parser = argparse.ArgumentParser(prog="cm", description="Coding Master v3")
    _add_global_args(parser, is_parent=True)
    sub = parser.add_subparsers(dest="command")

    # repos (no --repo required)
    sub.add_parser("repos", help="List configured repos and workspaces")

    # start
    p_start = sub.add_parser("start", help="One-shot: lock + plan + plan-ready")
    _add_global_args(p_start)
    p_start.add_argument("--branch", default=None)
    p_start.add_argument("--plan-file", default=None, help="Path to PLAN.md to copy")
    p_start.add_argument("--mode", default="deliver", choices=list(MODES.keys()),
                         help="Session mode: deliver, review, debug, analyze")

    # lock
    p_lock = sub.add_parser("lock", help="Lock workspace")
    _add_global_args(p_lock)
    p_lock.add_argument("--branch", default=None)
    p_lock.add_argument("--mode", default="deliver", choices=list(MODES.keys()),
                        help="Session mode: deliver, review, debug, analyze")
    p_lock.add_argument("--stash", action="store_true",
                        help="(deprecated, no-op) Session worktree is always clean")

    # unlock
    unlock_parser = sub.add_parser("unlock", help="Release lock")
    unlock_parser.add_argument("--force", action="store_true", help="Force unlock even if write session in progress")
    _add_global_args(unlock_parser)

    # status
    _add_global_args(sub.add_parser("status", help="Show lock status"))

    # renew
    _add_global_args(sub.add_parser("renew", help="Renew lease"))

    # plan-ready
    _add_global_args(sub.add_parser("plan-ready", help="Validate PLAN.md"))

    # claim
    p_claim = sub.add_parser("claim", help="Claim a feature")
    _add_global_args(p_claim)
    p_claim.add_argument("--feature", "-f", required=True, type=int)

    # scope (review/debug/analyze modes)
    p_scope = sub.add_parser("scope", help="Define analysis/review scope")
    _add_global_args(p_scope)
    p_scope.add_argument("--diff", default=None, help="Diff range (e.g. HEAD~3..HEAD)")
    p_scope.add_argument("--files", nargs="*", default=None, help="File paths or globs")
    p_scope.add_argument("--pr", default=None, help="PR number or URL")
    p_scope.add_argument("--goal", default=None, help="What to look for / investigate")

    # report (review/debug/analyze modes)
    p_report = sub.add_parser("report", help="Write session report or diagnosis")
    _add_global_args(p_report)
    p_report.add_argument("--content", default=None, help="Report content (inline)")
    p_report.add_argument("--file", default=None, help="Path to report file to copy")

    # engine-run
    p_engine = sub.add_parser("engine-run", help="Delegate analysis to engine subprocess")
    _add_global_args(p_engine)
    p_engine.add_argument("--goal", default=None, help="Analysis goal (overrides scope goal)")
    p_engine.add_argument("--engine", default="claude-code", help="Engine to use (default: claude-code)")
    p_engine.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    p_engine.add_argument("--max-turns", type=int, default=30, dest="max_turns",
                          help="Max engine turns (default: 30)")

    # delegate-prepare
    p_delegate_prepare = sub.add_parser("delegate-prepare", help="Prepare delegation request for a feature")
    _add_global_args(p_delegate_prepare)
    p_delegate_prepare.add_argument("--feature", "-f", required=True, type=int)

    # delegate-complete
    p_delegate_complete = sub.add_parser("delegate-complete", help="Mark delegation complete when artifacts exist")
    _add_global_args(p_delegate_complete)
    p_delegate_complete.add_argument("--feature", "-f", required=True, type=int)

    # dev
    p_dev = sub.add_parser("dev", help="Advance to developing")
    _add_global_args(p_dev)
    p_dev.add_argument("--feature", "-f", required=True, type=int)

    # test
    p_test = sub.add_parser("test", help="Run tests for feature")
    _add_global_args(p_test)
    p_test.add_argument("--feature", "-f", required=True, type=int)

    # done
    p_done = sub.add_parser("done", help="Mark feature done")
    _add_global_args(p_done)
    p_done.add_argument("--feature", "-f", required=True, type=int)

    # reopen
    p_reopen = sub.add_parser("reopen", help="Reopen done feature")
    _add_global_args(p_reopen)
    p_reopen.add_argument("--feature", "-f", required=True, type=int)

    # integrate
    _add_global_args(sub.add_parser("integrate", help="Merge + integration tests"))

    # submit
    p_submit = sub.add_parser("submit", help="Push + PR + cleanup")
    _add_global_args(p_submit)
    p_submit.add_argument("--title", "-t", required=True)

    # progress
    _add_global_args(sub.add_parser("progress", help="Show progress + action guidance"))

    # journal
    p_journal = sub.add_parser("journal", help="Append to JOURNAL.md")
    _add_global_args(p_journal)
    p_journal.add_argument("--message", "-m", required=True)

    # doctor
    p_doctor = sub.add_parser("doctor", help="Diagnose + fix state")
    _add_global_args(p_doctor)
    p_doctor.add_argument("--fix", action="store_true")

    args = parser.parse_args()

    # Commands that don't require --repo
    no_repo_commands = {"repos"}

    # Auto-detect repo from cwd if not specified
    if args.command not in no_repo_commands and not args.repo:
        cwd = Path.cwd()
        if (cwd / ".git").exists():
            args.repo = cwd.name
        else:
            _fail("--repo required (or run from within a git repo)")

    commands = {
        "repos": cmd_repos,
        "start": cmd_start,
        "lock": cmd_lock,
        "unlock": cmd_unlock,
        "status": cmd_status,
        "renew": cmd_renew,
        "plan-ready": cmd_plan_ready,
        "claim": cmd_claim,
        "scope": cmd_scope,
        "report": cmd_report,
        "engine-run": cmd_engine_run,
        "delegate-prepare": cmd_delegate_prepare,
        "delegate-complete": cmd_delegate_complete,
        "dev": cmd_dev,
        "test": cmd_test,
        "done": cmd_done,
        "reopen": cmd_reopen,
        "integrate": cmd_integrate,
        "submit": cmd_submit,
        "progress": cmd_progress,
        "journal": cmd_journal,
        "doctor": cmd_doctor,
    }

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handler = commands.get(args.command)
    if not handler:
        _fail(f"unknown command: {args.command}")

    try:
        # Mode gate: check command is allowed in current session mode
        # Skip for commands that don't require a lock (lock, start, status, doctor, unlock)
        no_gate_commands = {"lock", "start", "status", "doctor", "unlock", "repos"}
        if args.command not in no_gate_commands:
            repo = _repo_path(args.repo)
            lock_path = repo / CM_DIR / "lock.json"
            if lock_path.exists():
                gate_err = _check_mode_gate(repo, args.command)
                if gate_err:
                    _output(gate_err)
                    sys.exit(1)

        result = handler(args)
        _output(result)
    except SystemExit:
        raise
    except Exception as exc:
        _output({"ok": False, "error": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
