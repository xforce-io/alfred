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
import shutil
import socket
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── Add scripts dir to path so we can import siblings ──
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config_manager import ConfigManager
from integration_failure_classifier import IntegrationFailureClassifier

CM_DIR = ".coding-master"
SESSION_FILE = "session.json"       # persistent session history (survives unlock)
EVIDENCE_DIR = "evidence"
LAST_SESSION_FILE = "last_session.json"  # snapshot of completed session (survives unlock)
DELEGATION_DIR = "delegation"
LEASE_MINUTES = 120
READ_ONLY_MODES = {"review", "analyze"}  # These modes must not modify git state
TEST_OUTPUT_MAX = 500
# Commands that require session_phase in ("working", "integrating")
# Note: "claim" is excluded — it has its own check allowing "reviewed" or "working"
_WORKING_PHASE_COMMANDS = frozenset({"dev", "test", "done", "reopen", "integrate"})

# ── Mode definitions: constraints, not pipelines ──
MODES = {
    "deliver": {
        "required_artifacts": [],  # checked per-feature via evidence/N-verify.json
        "allowed_commands": [
            "lock", "unlock", "status", "renew", "start",
            "plan-ready", "claim", "dev", "test", "done", "reopen",
            "integrate", "submit", "progress", "journal", "doctor",
            "delegate-prepare", "delegate-complete",
            "engine-run", "change-summary",
            "read", "find", "grep", "edit",
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
            "engine-run", "change-summary",
            "read", "find", "grep",
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
            "engine-run", "change-summary",
            "read", "find", "grep", "edit",
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
            "engine-run", "change-summary",
            "read", "find", "grep",
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
            payload = data if result.get("ok", True) else (snapshot if data != snapshot else None)
            if payload is not None:
                serialized = json.dumps(payload, indent=2, ensure_ascii=False)
                f.seek(0)
                f.truncate()
                f.write(serialized)
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
    """Resolve repo and optionally verify lock exists."""
    repo = _repo_path(args.repo)
    require_lock = getattr(args, "require_lock", True)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if require_lock and not lock:
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


def _find_checked_out_branch_path(repo: Path, branch: str) -> Path | None:
    """Return the worktree path where branch is currently checked out, if any."""
    result = _run_git(repo, ["worktree", "list", "--porcelain"], check=False)
    if result.returncode != 0:
        return None

    worktree_path: Path | None = None
    branch_ref = f"refs/heads/{branch}"
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktree_path = Path(line.removeprefix("worktree ").strip())
            continue
        if line.startswith("branch ") and line.removeprefix("branch ").strip() == branch_ref:
            return worktree_path
    return None


def _read_session(repo: Path) -> dict:
    """Read persistent session history from .coding-master/session.json."""
    return _atomic_json_read(repo / CM_DIR / SESSION_FILE)


def _save_session(repo: Path, branch: str, mode: str, phase: str):
    """Persist session state that must survive across lock/unlock cycles.

    This is the single source of truth for "what branch was this repo
    working on" — read by cmd_lock when deciding whether to create a
    new branch or continue on an existing one.
    """
    session_path = repo / CM_DIR / SESSION_FILE

    def update(data):
        data.update({
            "branch": branch,
            "mode": mode,
            "phase": phase,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"ok": True}

    session_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_update(session_path, update)


def _save_last_session(repo: Path, lock: dict, **extra):
    """Snapshot the completing session so cm progress can report it after unlock."""
    mode = lock.get("mode", "unknown")
    summary: dict = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "branch": lock.get("branch", ""),
    }

    if mode == "deliver":
        claims = _atomic_json_read(repo / CM_DIR / "claims.json")
        plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
        features = []
        for fid, spec in plan.items():
            claim = claims.get("features", {}).get(fid, {})
            dev = claim.get("developing", {})
            evidence = _read_evidence(repo, fid)
            features.append({
                "id": fid,
                "title": spec.get("title", ""),
                "phase": claim.get("phase", "pending"),
                "test_status": evidence.get("overall") if evidence else dev.get("test_status", "pending"),
            })
        summary["features"] = features
    else:
        # review / debug / analyze — include scope
        scope_path = repo / CM_DIR / "scope.json"
        if scope_path.exists():
            try:
                summary["scope"] = json.loads(scope_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    summary.update(extra)
    last_path = repo / CM_DIR / LAST_SESSION_FILE
    last_path.parent.mkdir(parents=True, exist_ok=True)
    last_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))


def _reuse_session_branch(repo: Path) -> str | None:
    """Return the branch from session.json if it still points at HEAD.

    This is the key mechanism that prevents branch proliferation: instead
    of creating dev/alfred-0312-1231, dev/alfred-0312-1232, ... on every
    lock cycle, we continue working on the same branch as long as it
    hasn't diverged.
    """
    session = _read_session(repo)
    branch = session.get("branch", "")
    if not branch:
        return None
    try:
        head_sha = _run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        branch_sha = _run_git(repo, ["rev-parse", branch], check=False).stdout.strip()
        if head_sha and head_sha == branch_sha:
            return branch
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return None


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

    # Guard: reject branches already checked out in the main repo (e.g. branch="main").
    # This prevents the opaque "fatal: 'main' is already checked out" git error.
    existing_checkout = _find_checked_out_branch_path(repo, branch)
    if existing_checkout and existing_checkout.resolve() == repo.resolve():
        return {
            "ok": False,
            "error": (
                f"Cannot create session worktree: branch '{branch}' is already checked out "
                f"in the main repo at '{repo}'. "
                "Run _cm_doctor(repo=..., fix=True) to reset the session, "
                "then call _cm_next(repo=...) to restart."
            ),
        }

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


def _reset_plan_layer(repo: Path):
    """Clean stale plan-layer state when creating a new session.

    Removes per-session files (PLAN.md, claims.json, features/, evidence/)
    while preserving cross-session files (session.json, JOURNAL.md).
    """
    import shutil
    cm = repo / CM_DIR
    if not cm.is_dir():
        return
    # Per-session files to remove
    for name in ("PLAN.md", "claims.json", "engine-attempts.json",
                 "engine_result.json", "scope.json", "CRITICAL_ISSUES.md"):
        p = cm / name
        if p.exists():
            p.unlink()
    # Per-session directories to remove
    for name in ("features", "evidence", "delegation"):
        p = cm / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower().strip())
    s = re.sub(r"[\s_]+", "-", s)
    return s[:30] or "feature"


_DIFF_RANGE_PATTERN = re.compile(r"^[\w\-./~^:@]+$")


def _is_valid_diff_range(diff_range: str) -> bool:
    """
    Validate diff_range to prevent argument injection.
    Only allows valid git revision range characters:
    - alphanumeric, hyphen, dot, slash, tilde, caret, colon, at
    - rejects shell metacharacters, semicolons, pipes, redirections
    """
    if not diff_range or len(diff_range) > 200:
        return False
    # Check for shell metacharacters and option injection patterns
    dangerous_chars = set(";|&$`'\"\\<>()*?[]{}\n\r\t")
    if any(c in diff_range for c in dangerous_chars):
        return False
    # Check for option injection (e.g., --output, -o)
    if diff_range.startswith("-"):
        return False
    # Validate against allowed pattern
    parts = diff_range.split("..")
    if len(parts) > 2:
        return False
    for part in parts:
        part = part.lstrip("^")  # Allow ^ prefix for negation
        if not part:
            continue
        if not _DIFF_RANGE_PATTERN.match(part):
            return False
    return True


def _run_git(repo: Path, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, return CompletedProcess."""
    return subprocess.run(
        ["git"] + cmd, cwd=str(repo), capture_output=True, text=True,
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


def _parse_max_features(path: Path) -> int:
    """Parse `## Max Features: N` from PLAN.md. Returns 1 if not declared."""
    if not path.exists():
        return 1
    for line in path.read_text().splitlines():
        m = re.match(r"^## Max Features:\s*(\d+)", line, re.IGNORECASE)
        if m:
            return max(1, min(int(m.group(1)), 10))  # clamp to [1, 10]
    return 1


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
#  Project config (.coding-master.toml)
# ══════════════════════════════════════════════════════════


def _load_project_config(cwd: Path) -> dict:
    """Load .coding-master.toml from project root. Returns {} if missing."""
    cfg_path = cwd / ".coding-master.toml"
    if not cfg_path.is_file():
        return {}
    if tomllib is None:
        logger.warning(".coding-master.toml found but tomllib unavailable (Python < 3.11)")
        return {}
    try:
        return tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", cfg_path, exc)
        return {}


# ══════════════════════════════════════════════════════════
#  Test execution
# ══════════════════════════════════════════════════════════


def _run_tests(cwd: Path, cmd_override: str | None = None) -> dict:
    """Run tests in the given directory. Returns {ok, output}."""
    from test_runner import _exec, _parse_pytest_output, _resolve_pytest_command

    test_cmd = cmd_override
    if not test_cmd:
        # Auto-detect test command
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


def _run_lint(cwd: Path, cmd_override: str | None = None) -> dict:
    """Run lint in the given directory. Returns {passed, command, output}."""
    from test_runner import _exec, _find_venv_binary, _has_tool

    lint_cmd = cmd_override
    if not lint_cmd:
        if (cwd / "pyproject.toml").exists():
            if _has_tool(cwd / "pyproject.toml", "ruff"):
                venv_ruff = _find_venv_binary(cwd, "ruff")
                lint_cmd = f"{venv_ruff.resolve()} check ." if venv_ruff else "ruff check ."
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


def _run_typecheck(cwd: Path, cmd_override: str | None = None) -> dict:
    """Run typecheck in the given directory. Returns {passed, command, output}."""
    from test_runner import _exec

    tc_cmd = cmd_override or _resolve_typecheck_command(cwd)
    if not tc_cmd:
        return {"passed": True, "skipped": True, "command": None, "output": "no typecheck command detected (skipped)"}

    stdout, stderr, rc = _exec(str(cwd), tc_cmd)
    combined = stdout + stderr
    output = combined[-TEST_OUTPUT_MAX:] if len(combined) > TEST_OUTPUT_MAX else combined
    return {"passed": rc == 0, "command": tc_cmd, "output": output}


def _resolve_typecheck_command(cwd: Path) -> str | None:
    """Detect typecheck command for a project."""
    from test_runner import _find_venv_binary, _has_tool

    if (cwd / "pyproject.toml").exists():
        venv_mypy = _find_venv_binary(cwd, "mypy")
        if _has_tool(cwd / "pyproject.toml", "mypy"):
            if venv_mypy:
                return f"{venv_mypy.resolve()} ."
            return "mypy ."
        if venv_mypy:
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


def _precondition_check(repo: Path, feature_id: str | None = None, *, command: str | None = None) -> dict | None:
    """Check preconditions before mutation commands.

    Returns error dict if precondition violated, None if OK.
    Checks: lease validity, branch consistency, session not done,
    mode gate, session phase gate.
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

    # 4. Mode gate: command allowed in current session mode
    if command:
        mode = lock.get("mode", "deliver")
        mode_def = MODES.get(mode)
        if mode_def and command not in mode_def["allowed_commands"]:
            return {
                "ok": False,
                "error": f"command '{command}' not available in '{mode}' mode",
                "data": {
                    "mode": mode,
                    "allowed_commands": mode_def["allowed_commands"],
                    "description": mode_def["description"],
                },
            }

    # 5. Session phase gate: mutation commands require "working", "integrating", or "reviewing"
    if command and command in _WORKING_PHASE_COMMANDS:
        sp = lock.get("session_phase")
        if sp not in ("working", "integrating", "reviewing"):
            return {"ok": False, "error": f"session is '{sp}', command '{command}' requires "
                    f"a claimed feature (session_phase: working or integrating)"}

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
    "deliver":  _hint("_cm_next", "Call _cm_next — it will auto-validate PLAN.md and advance the session"),
    "review":   _hint("_cm_next(intent='scope', diff='HEAD~3..HEAD')", "Define what to review"),
    "analyze":  _hint("_cm_next(intent='scope', files='...')", "Define what to analyze"),
    "debug":    _hint("_cm_next(intent='scope', diff='HEAD~3..HEAD')", "Define what to investigate"),
}

_FLOW_AFTER_SCOPE = _hint("_cm_next", "Call _cm_next — engine will run automatically and return findings")

_FLOW_AFTER_ENGINE = {
    "review":   _hint("_cm_edit --file '.coding-master/report.md' --old_text '' --new_text '...'", "Write review report based on engine findings"),
    "analyze":  _hint("_cm_edit --file '.coding-master/report.md' --old_text '' --new_text '...'", "Write analysis report based on engine findings"),
    "debug":    _hint("_cm_edit --file '.coding-master/diagnosis.md' --old_text '' --new_text '...'", "Write diagnosis based on engine findings"),
}

_FLOW_AFTER_REPORT = _hint(
    "_cm_next",
    "Diagnosis written. If user wants fixes applied, use _cm_edit (stay in debug session); "
    "call _cm_next again after edits to complete and auto-unlock."
)


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

    # Pre-compute branch info outside atomic section (avoid subprocess under flock).
    current_branch = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    reuse_branch = None if read_only else _reuse_session_branch(repo)
    explicit_branch = getattr(args, "branch", None) if not read_only else None
    active_lock = _atomic_json_read(lock_path)
    checked_out_path = (
        _find_checked_out_branch_path(repo, explicit_branch)
        if explicit_branch and not active_lock.get("session_phase")
        else None
    )

    if checked_out_path:
        checked_out_hint = (
            "Omit the branch parameter to auto-generate a dev branch."
            if checked_out_path.resolve() == repo.resolve()
            else "Use a different branch name or unlock the existing session first."
        )
        return {
            "ok": False,
            "error": (
                f"Cannot use branch '{explicit_branch}' because it is already checked out at "
                f"'{checked_out_path}'. {checked_out_hint}"
            ),
        }

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

            # Upgrade: review/analyze → debug
            existing_mode = data.get("mode")
            if not read_only and existing_read_only:
                if existing_mode in ("review", "analyze") and mode == "debug":
                    data["mode"] = "debug"
                    data["read_only"] = False
                    data["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
                    agents = data.setdefault("session_agents", [])
                    if agent not in agents:
                        agents.append(agent)
                    action_taken["type"] = "upgraded"
                    return {"ok": True, "data": dict(data)}
                else:
                    return {"ok": False, "error": f"cannot upgrade from {existing_mode} to {mode}. "
                            f"Only review/analyze → debug is supported."}

            # Same mode family: join the session
            data["lease_expires_at"] = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
            agents = data.setdefault("session_agents", [])
            if agent not in agents:
                agents.append(agent)
            action_taken["type"] = "joined"
            return {"ok": True, "data": dict(data), "hint": "session resumed"}

        # ── No session or session done → create new ──
        branch = current_branch if read_only else (
            explicit_branch
            or reuse_branch
            or f"dev/{args.repo}-{now.strftime('%m%d-%H%M')}"
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

    # ── Upgraded from read-only to debug: create dev branch + session worktree ──
    if action_taken["type"] == "upgraded":
        lock = _atomic_json_read(lock_path)
        # Read-only sessions use main as branch; upgrade needs a new dev branch
        now = datetime.now(timezone.utc)
        dev_branch = (
            reuse_branch
            or f"dev/{args.repo}-{now.strftime('%m%d-%H%M')}"
        )
        _atomic_json_update(lock_path, lambda d: (
            d.update({"branch": dev_branch}), {"ok": True}
        )[1])
        lock["branch"] = dev_branch
        session_result = _ensure_session_worktree(repo, lock)
        if not session_result.get("ok"):
            return session_result
        session_wt = session_result["data"]["session_worktree"]
        _save_session(repo, dev_branch, "debug", "locked")
        _append_journal(repo, agent, "lock",
                        f"Upgraded to debug mode, branch: {dev_branch}, worktree: {session_wt}")
        return {"ok": True, "data": {
            "branch": dev_branch,
            "session_worktree": session_wt,
            "mode": "debug",
            "upgraded": True,
            "next_action": _FLOW_AFTER_LOCK.get("debug"),
        }}

    # ── Joined existing session: validate or recover session worktree ──
    if action_taken["type"] == "joined":
        existing_data = result.get("data", {})
        existing_branch = existing_data.get("branch", "")
        if not existing_data.get("read_only", False):
            session_result = _ensure_session_worktree(repo, existing_data)
            if not session_result.get("ok"):
                return session_result
            session_wt = session_result["data"]["session_worktree"]
            existing_data["session_worktree"] = session_wt
            # Detect and fix worktree branch mismatch (e.g. manual checkout)
            if session_wt and existing_branch:
                try:
                    actual = _run_git(
                        Path(session_wt),
                        ["rev-parse", "--abbrev-ref", "HEAD"],
                        check=False,
                    ).stdout.strip()
                    if actual and actual != existing_branch:
                        _run_git(Path(session_wt), ["checkout", existing_branch], check=False)
                        existing_data["branch_mismatch"] = {
                            "expected": existing_branch,
                            "was": actual,
                            "action": "auto-checkout",
                        }
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass
        _append_journal(repo, agent, "lock", f"Joined session, branch: {existing_branch}")
        existing_data["next_action"] = next_action
        return {"ok": True, "data": existing_data}

    # ── New session created: clean stale plan-layer state ──
    _ensure_gitignore(repo)
    _reset_plan_layer(repo)

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
    _save_session(repo, branch, mode, "locked")
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
        return {"ok": True, "next_action": _hint("cm lock", "No active lock. Call cm lock before any file operations.")}  # already unlocked

    force = getattr(args, "force", False)
    if not force and not lock.get("read_only", False):
        phase = lock.get("session_phase", "")
        if phase and phase != "done":
            return {"ok": False,
                    "error": f"write session in progress (phase={phase}). "
                             "Use cm submit to complete, or cm unlock --force to discard."}

    # Snapshot session before cleanup (best effort)
    if not lock.get("read_only", False):
        try:
            _save_last_session(repo, lock)
        except Exception as exc:
            logger.warning("Failed to save last session snapshot: %s", exc)

    # Cleanup session worktree (best effort): force unlock or completed session
    session_wt = lock.get("session_worktree", "")
    if session_wt and (force or lock.get("session_phase") == "done"):
        _remove_worktree(repo, session_wt)

    def clear_lock(data):
        data.clear()
        return {"ok": True, "next_action": _hint(
            "cm lock",
            "Lock released. Call cm lock --mode <deliver|debug|review|analyze> before any file operations."
        )}

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
        return "lease expired", "cm start (re-joins and renews) or cm unlock"

    # Priority 2: integration failed
    report_path = repo / CM_DIR / EVIDENCE_DIR / "integration-report.json"
    if lock.get("session_phase") in ("integrating", "reviewing"):
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
        return {
            "ok": False,
            "error": (
                "PLAN.md contains no parseable features. "
                "Required format — each feature must look exactly like this:\n\n"
                "### Feature 1: Short title\n"
                "**Depends on**: —\n\n"
                "#### Task\n"
                "Describe what needs to be done.\n\n"
                "#### Acceptance Criteria\n"
                "- [ ] criterion one\n"
                "- [ ] criterion two\n\n"
                "The headings '### Feature N:', '#### Task', and '#### Acceptance Criteria' "
                "are REQUIRED and must be spelled exactly as shown. "
                "Use _cm_edit to fix PLAN.md, then call _cm_next again."
            ),
        }

    # Feature count constraint: default 1, overridable via `## Max Features: N` in PLAN.md
    max_features = _parse_max_features(plan_path)
    if len(plan) > max_features:
        return {
            "ok": False,
            "error": (
                f"PLAN.md has {len(plan)} features but max_features={max_features}. "
                f"Merge into {max_features} feature(s), or add '## Max Features: N' "
                f"(with a justification line) after '## Origin Task' to raise the limit. "
                f"Analysis/scan work should NOT be a separate feature — "
                f"it belongs in the engine's analyze phase."
            ),
        }

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
    pre_err = _precondition_check(repo, command="claim")
    if pre_err:
        return pre_err

    # Check session_phase
    lock = _atomic_json_read(lock_path)
    if lock.get("session_phase") == "locked":
        plan_path = repo / CM_DIR / "PLAN.md"
        if plan_path.exists():
            return {"ok": False, "error": "session is locked but PLAN.md exists. "
                    "You must advance to reviewed phase before claiming.",
                    "next_action": {"tool": "_cm_next", "args": {"repo": args.repo}},
                    "hint": "Call _cm_next — it will auto-validate PLAN.md and advance the session."}
        return {"ok": False, "error": "session is locked and PLAN.md does not exist yet.",
                "next_action": {"tool": "_cm_next", "args": {"repo": args.repo}},
                "hint": "Call _cm_next — it will guide you through creating PLAN.md and advancing the session."}
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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
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
    pre_err = _precondition_check(repo, feature_id, command="dev")
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
        worktree = _get_feature_worktree(claims_path, feature_id)
        result.setdefault("data", {})["worktree"] = worktree
        result["data"]["next_action"] = _hint(
            f"cm test --feature {feature_id}",
            "Write code and commit, then run tests")
    return result


def cmd_test(args) -> dict:
    """Run lint+typecheck+tests, write evidence + claims.json."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # Precondition check
    pre_err = _precondition_check(repo, feature_id, command="test")
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

    # Verify worktree exists
    if not wt_path.exists():
        return {"ok": False, "error": f"Worktree {wt_path} does not exist. "
                f"Run cm claim --feature {feature_id} to recreate it."}

    # Verify no uncommitted changes to tracked files (untracked files are OK)
    git_status = _run_git(wt_path, ["status", "--porcelain", "-uno"], check=False)
    if git_status.returncode != 0:
        return {"ok": False, "error": f"git status failed: {git_status.stderr.strip()}"}
    if git_status.stdout.strip():
        return {"ok": False, "error": "uncommitted changes to tracked files, commit before testing"}

    # Get HEAD + commit count
    head_result = _run_git(wt_path, ["rev-parse", "HEAD"], check=False)
    if head_result.returncode != 0:
        return {"ok": False, "error": f"git rev-parse HEAD failed: {head_result.stderr.strip()}"}
    head = head_result.stdout.strip()
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    dev_branch = lock.get("branch", "HEAD")
    raw_count = _run_git(
        wt_path, ["rev-list", "--count", f"{dev_branch}..HEAD"], check=False
    ).stdout.strip()
    commit_count = int(raw_count) if raw_count.isdigit() else 0

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
    elif test_result["ok"]:
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

    # Write evidence BEFORE updating claims so cmd_done always finds it
    # even if the process crashes between these two operations.
    _write_evidence(repo, feature_id, evidence)

    result = _atomic_json_update(claims_path, update_test_state)
    if not result.get("ok"):
        _delete_evidence(repo, feature_id)
        return result
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
    pre_err = _precondition_check(repo, feature_id, command="done")
    if pre_err:
        return pre_err
    delegate_err = _check_delegation_gate(repo, feature_id)
    if delegate_err:
        return delegate_err

    # Read actual git HEAD (outside flock to avoid blocking)
    worktree = _get_feature_worktree(claims_path, feature_id)
    wt_path = Path(worktree) if worktree else repo
    if not wt_path.exists():
        return {"ok": False, "error": f"Worktree {wt_path} does not exist. "
                f"Run cm claim --feature {feature_id} to recreate it."}
    head_result = _run_git(wt_path, ["rev-parse", "HEAD"], check=False)
    current_head = head_result.stdout.strip() if head_result.returncode == 0 else None

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
                failed = [k for k in ("test",)
                          if not evidence.get(k, {}).get("passed", True)]
                return {"ok": False, "error": f"Verification failed: {', '.join(failed) or 'test'}. Fix and re-run cm test."}
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

        # Include change summary with diff and worktree path
        lock = _atomic_json_read(repo / CM_DIR / "lock.json")
        base_ref = lock.get("branch", "HEAD~1")
        try:
            data["change_summary"] = _build_change_summary(wt_path, base_ref)
        except Exception as exc:
            logger.debug("Failed to build change summary: %s", exc)

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
    pre_err = _precondition_check(repo, command="reopen")
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
    pre_err = _precondition_check(repo, command="integrate")
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
        feat_entry = claims["features"].get(fid, {})
        if feat_entry.get("phase") == "skipped":
            merge_results.append({"feature": fid, "status": "skipped"})
            continue
        fb = feat_entry.get("branch")
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
            classifier = IntegrationFailureClassifier(report)
            return {"ok": False, "error": f"merge failed ({fb}): {merge_rc.stderr.strip()}. "
                    "Run cm reopen for the conflicting feature, resolve, then retry",
                    "data": {"classification": classifier.summary()}}
        else:
            head_r = _run_git(wt, ["rev-parse", "HEAD"], check=False)
            commit = head_r.stdout.strip() if head_r.returncode == 0 else ""
            merge_results.append({"feature": fid, "branch": fb, "status": "merged", "commit": commit})

    # Run full tests on merged dev branch (in session worktree)
    test_result = _run_tests(wt)
    output_summary = (test_result.get("output", "") or "")[:1000]

    if not test_result["ok"]:
        reset_rc = subprocess.run(
            ["git", "reset", "--hard", pre_merge_sha],
            cwd=wt, capture_output=True,
        )
        if reset_rc.returncode != 0:
            logger.warning("Integration rollback failed: %s", reset_rc.stderr.decode())
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
        classifier = IntegrationFailureClassifier(report)
        return {"ok": False, "error": "integration tests failed",
                "data": {"output": output_summary,
                         "hint": "cm reopen → fix → cm test → cm done → retry cm integrate",
                         "classification": classifier.summary()}}

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

    phase = lock.get("session_phase")
    if phase == "reviewing":
        return {"ok": False, "error": "session is awaiting diff review; "
                "call _cm_next(intent='confirm') to approve and submit"}
    if phase != "integrating":
        return {"ok": False, "error": f"session is {phase}, run cm integrate first"}

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
    pr_warning = None
    if existing_pr.returncode != 0:
        pr_body = _generate_pr_body(repo)
        pr_result = subprocess.run(
            ["gh", "pr", "create", "--title", args.title, "--body", pr_body],
            cwd=wt, capture_output=True, text=True,
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
        else:
            err = (pr_result.stderr or pr_result.stdout).strip()
            pr_warning = (
                f"Branch pushed but PR creation failed: {err}. "
                "Run 'gh auth login' to re-authenticate, then create the PR manually with: "
                f"gh pr create --title '{args.title}' --head {branch}"
            )
            logger.warning("gh pr create failed: %s", err)
    else:
        try:
            pr_url = json.loads(existing_pr.stdout).get("url")
        except json.JSONDecodeError:
            pass

    # Capture change summary before cleanup removes worktrees
    try:
        # Use main branch as base for the full diff
        main_branch = _run_git(wt, ["symbolic-ref", "refs/remotes/origin/HEAD"], check=False).stdout.strip()
        if not main_branch:
            main_branch = "origin/main"
        else:
            main_branch = main_branch.replace("refs/remotes/", "")
        change_summary = _build_change_summary(wt, main_branch)
    except Exception as exc:
        logger.debug("Failed to build change summary for submit: %s", exc)
        change_summary = None

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
    _save_session(repo, branch, "deliver", "done")
    _append_journal(repo, agent, "submit", f"PR: {pr_url or branch}")

    # Snapshot last session with submit-specific extras (before unlock clears lock)
    submit_extras: dict = {}
    if pr_url:
        submit_extras["pr_url"] = pr_url
    if change_summary:
        submit_extras["change_summary"] = change_summary
    try:
        _save_last_session(repo, lock, **submit_extras)
    except Exception as exc:
        logger.warning("Failed to save last session snapshot after submit: %s", exc)

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
    features_skipped = sum(1 for f in claims.get("features", {}).values() if f.get("phase") == "skipped")
    evidence_dir = str(repo / CM_DIR / EVIDENCE_DIR)

    result_data = {
        "branch": branch, "pr_url": pr_url,
        "evidence_dir": evidence_dir,
        "features_completed": features_completed,
        "features_skipped": features_skipped,
        "features_total": features_total,
        "exit_status": "success",
        "journal": str(repo / CM_DIR / "JOURNAL.md"),
    }
    if change_summary:
        result_data["change_summary"] = change_summary
    response: dict = {"ok": True, "data": result_data}
    if pr_warning:
        response["warning"] = pr_warning
    return response


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
    # Inject metadata header so future agents can assess staleness.
    head_commit = _run_git(repo, ["rev-parse", "--short", "HEAD"], check=False).stdout.strip()
    meta_header = (
        f"<!-- generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        f"  commit: {head_commit}"
        f"  mode: {mode} -->\n\n"
    )
    report_path.write_text(meta_header + content)
    agent = _resolve_agent(args)
    _append_journal(repo, agent, "report", f"Report written: {filename}")

    # Auto-unlock for review/analyze only — debug keeps session open for fixes.
    auto_unlocked = False
    if mode in ("review", "analyze"):
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
    if auto_unlocked:
        result["data"]["next_action"] = _hint(
            "cm lock --mode deliver",
            "Session ended (auto-unlocked). If user wants to act on findings, "
            "re-lock with cm lock --mode deliver (full feature workflow) "
            "or cm lock --mode debug (quick targeted fix)."
        )
    else:
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
        # Validate diff_range to prevent argument injection
        if not _is_valid_diff_range(diff_range):
            return f"(invalid diff range: {diff_range})"
        try:
            result = _run_git(repo, ["diff", diff_range], check=False)
            diff_text = result.stdout
            if len(diff_text) > max_bytes:
                diff_text = diff_text[:max_bytes] + "\n...(diff truncated)..."
            return f"## Diff ({diff_range})\n```\n{diff_text}\n```"
        except subprocess.TimeoutExpired:
            return f"(git diff timed out for {diff_range})"
        except OSError as e:
            return f"(git diff failed: {e})"

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
        except subprocess.TimeoutExpired:
            return f"(gh pr diff timed out for PR #{pr})"
        except OSError as e:
            return f"(gh pr diff failed: {e})"

    return "(no specific scope context)"


# ── Engine-delegated deliver helpers (v5.1) ────────────────────────────────

MAX_ENGINE_RETRIES = 3

# Max turns per engine phase (more complex phases get more turns)
_ENGINE_MAX_TURNS = {"analyze": 15, "implement": 30, "fix": 20}


def _build_feature_engine_prompt(
    phase: str,
    spec: dict,
    feature_md_content: str,
    test_output: str = "",
) -> str:
    """Build engine prompt for a deliver sub-phase (analyze/implement/fix)."""
    title = spec.get("title", "")
    task = spec.get("task", "")
    criteria = spec.get("criteria", "")

    if phase == "analyze":
        return (
            "You are a senior engineer. Analyze the codebase to understand how to "
            f"implement the following feature, then write your analysis.\n\n"
            f"## Feature: {title}\n\n"
            f"### Task\n{task}\n\n"
            f"### Acceptance Criteria\n{criteria}\n\n"
            f"### Current Feature Markdown\n```\n{feature_md_content}\n```\n\n"
            "## Instructions\n"
            "1. Read the relevant source code to understand the current implementation.\n"
            "2. Edit the feature markdown file above — fill in the `## Analysis` section "
            "with your findings and the `## Plan` section with numbered implementation steps.\n"
            "3. Do NOT modify any source code in this phase.\n"
        )
    elif phase == "implement":
        return (
            "You are a senior engineer. Implement the feature described below.\n\n"
            f"## Feature: {title}\n\n"
            f"### Task\n{task}\n\n"
            f"### Acceptance Criteria\n{criteria}\n\n"
            f"### Analysis & Plan\n```\n{feature_md_content}\n```\n\n"
            "## Instructions\n"
            "1. Follow the Plan in the feature markdown above.\n"
            "2. Edit source files to implement the feature.\n"
            "3. Keep changes minimal and focused.\n"
            "4. Do NOT run tests yourself — the system will run them after you finish.\n"
        )
    elif phase == "fix":
        return (
            "You are a senior engineer. Tests are failing after a code change. "
            "Fix the code so all tests pass.\n\n"
            f"## Feature: {title}\n\n"
            f"### Task\n{task}\n\n"
            f"### Test Output (failures)\n```\n{test_output}\n```\n\n"
            "## Instructions\n"
            "1. Read the failing test output above.\n"
            "2. Find and fix the root cause in the source code.\n"
            "3. Keep fixes minimal — only change what's needed to make tests pass.\n"
            "4. Do NOT run tests yourself — the system will re-run them after you finish.\n"
        )
    else:
        return f"Implement the feature: {title}\nTask: {task}\n"


def _get_diff_summary(session_wt: Path, base_branch: str = "main") -> dict:
    """Compute git diff summary between session worktree HEAD and base branch."""
    try:
        # Use merge-base so we only show changes introduced in this session
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", base_branch],
            cwd=session_wt, capture_output=True, text=True, timeout=30,
        )
        base_ref = merge_base.stdout.strip() if merge_base.returncode == 0 else base_branch

        stat = subprocess.run(
            ["git", "diff", f"{base_ref}..HEAD", "--stat"],
            cwd=session_wt, capture_output=True, text=True, timeout=30,
        )
        diff = subprocess.run(
            ["git", "diff", f"{base_ref}..HEAD"],
            cwd=session_wt, capture_output=True, text=True, timeout=30,
        )
        diff_text = diff.stdout
        if len(diff_text) > 8000:
            diff_text = diff_text[:8000] + "\n... [truncated]"

        files_changed = []
        insertions = 0
        deletions = 0
        for line in stat.stdout.splitlines():
            if "|" in line:
                files_changed.append(line.split("|")[0].strip())
            m = re.search(r"(\d+) insertion", line)
            if m:
                insertions = int(m.group(1))
            m = re.search(r"(\d+) deletion", line)
            if m:
                deletions = int(m.group(1))

        return {
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
            "diff_stat": stat.stdout.strip()[:2000],
            "diff_text": diff_text,
        }
    except Exception as exc:
        return {"error": str(exc), "files_changed": [], "insertions": 0, "deletions": 0,
                "diff_stat": "", "diff_text": ""}


def _run_engine_for_feature(
    repo: Path,
    feature_id: str,
    phase: str,
    args,
    test_output: str = "",
    worktree_override: Path | None = None,
) -> dict:
    """Run the engine (claude-code CLI) for a deliver-mode feature phase.

    Unlike cmd_engine_run (scope-based for review/analyze), this builds a
    feature-specific prompt and runs in the feature worktree.
    """
    from engine import get_engine

    try:
        # Read feature data
        claims = _atomic_json_read(repo / CM_DIR / "claims.json") or {}
        feat = claims.get("features", {}).get(feature_id, {})
        wt = worktree_override or Path(feat.get("worktree", str(repo)))
        if not wt.exists():
            return {"ok": False, "error": f"worktree {wt} does not exist"}

        # Read feature spec from PLAN.md
        plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
        spec = plan.get(feature_id, {})

        # Read current feature markdown
        feat_md = _find_feature_md(repo, feature_id)
        feat_md_content = feat_md.read_text() if feat_md and feat_md.exists() else ""

        # Build prompt
        prompt = _build_feature_engine_prompt(phase, spec, feat_md_content, test_output)

        # Get engine
        engine_name = getattr(args, "engine", "claude-code")
        try:
            engine = get_engine(engine_name)
        except Exception as exc:
            return {"ok": False, "error": f"engine '{engine_name}' not available: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"setup error in _run_engine_for_feature: {exc}"}

    max_turns = _ENGINE_MAX_TURNS.get(phase, 30)
    timeout = getattr(args, "timeout", 600)

    agent = _resolve_agent(args)
    _append_journal(repo, agent, f"engine-{phase}",
                    f"Feature {feature_id}: engine {phase} start (engine={engine_name})")

    try:
        result = engine.run(
            prompt=prompt,
            repo_path=wt,
            mode="deliver",
            timeout=timeout,
            max_turns=max_turns,
        )
    except Exception as exc:
        _append_journal(repo, agent, f"engine-{phase}-error",
                        f"Feature {feature_id}: engine error: {exc}")
        return {"ok": False, "error": f"engine error: {exc}"}

    _append_journal(repo, agent, f"engine-{phase}-done",
                    f"Feature {feature_id}: engine {phase} done "
                    f"(ok={result.ok}, files_changed={len(result.files_changed)})")

    if not result.ok:
        return {"ok": False, "error": result.error or "engine returned failure",
                "summary": result.summary}

    return {"ok": True, "summary": result.summary,
            "files_changed": result.files_changed}


def _get_engine_retry_count(repo: Path, feature_id: str, phase: str) -> int:
    """Read engine retry count from lock.json."""
    lock = _atomic_json_read(repo / CM_DIR / "lock.json") or {}
    return lock.get("_engine_retries", {}).get(f"{feature_id}:{phase}", 0)


def _increment_engine_retry(repo: Path, feature_id: str, phase: str) -> None:
    """Increment engine retry count in lock.json."""
    key = f"{feature_id}:{phase}"
    def updater(data):
        retries = data.setdefault("_engine_retries", {})
        retries[key] = retries.get(key, 0) + 1
        return {"ok": True}
    _atomic_json_update(repo / CM_DIR / "lock.json", updater)


def _reset_engine_retries(repo: Path, feature_id: str) -> None:
    """Reset all retry counters for a feature (called on phase success)."""
    def updater(data):
        retries = data.get("_engine_retries", {})
        for key in list(retries):
            if key.startswith(f"{feature_id}:"):
                del retries[key]
        return {"ok": True}
    _atomic_json_update(repo / CM_DIR / "lock.json", updater)


def _auto_commit(wt: Path, message: str = "wip: coding-master auto-commit") -> None:
    """Auto-commit any changes in a worktree. Mechanical step after engine edits."""
    if not wt.exists():
        return
    gs = _run_git(wt, ["status", "--porcelain", "-uno"], check=False)
    if gs.returncode == 0 and gs.stdout.strip():
        _run_git(wt, ["add", "-A", "--", ":(exclude).coding-master"], check=False)
        _run_git(wt, ["commit", "-m", message], check=False)


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
    else:
        # Engine failed — provide fallback hint so agent can analyze manually
        scope_data = _atomic_json_read(scope_path) if scope_path.exists() else {}
        data["fallback_hint"] = {
            "message": "Engine failed. Use cm read/grep/find to analyze manually.",
            "suggested_steps": [
                "cm find --pattern '**/*.py' to locate relevant files",
                "cm grep --pattern '<keyword>' to search for code patterns",
                "cm read --file <path> to read specific files",
            ],
            "scope_files": scope_data.get("files", []),
        }
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
    repo = _repo_path(args.repo)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")

    # No active session — return last session snapshot if available
    if not lock:
        last_path = repo / CM_DIR / LAST_SESSION_FILE
        if last_path.exists():
            try:
                last = json.loads(last_path.read_text())
                return {"ok": True, "data": {
                    "active_session": False,
                    "last_session": last,
                }}
            except (json.JSONDecodeError, OSError):
                pass
        return {"ok": True, "data": {"active_session": False}}

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
    if session_phase == "reviewing":
        return ["Review diff, then _cm_next(intent='confirm') or _cm_next(intent='fix', feedback='...')"]
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


def cmd_regression(args) -> dict:
    """Run full regression (lint + typecheck + tests) on session worktree.

    Unlike ``cm test --feature N`` which operates on a feature worktree and
    writes evidence/claims, this runs on the session worktree (or repo root)
    and only returns results — no state mutation.

    Command overrides are read from ``.coding-master.toml`` in the project
    root (keys: ``[test] command``, ``[lint] command``, ``[typecheck] command``).
    """
    repo = _resolve_locked_repo(args)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    wt = _get_session_worktree(repo, lock) or repo

    # Load per-project config overrides
    cfg = _load_project_config(wt)
    test_cmd = cfg.get("test", {}).get("command")
    lint_cmd = cfg.get("lint", {}).get("command")
    tc_cmd = cfg.get("typecheck", {}).get("command")

    lint_result = _run_lint(wt, cmd_override=lint_cmd)
    typecheck_result = _run_typecheck(wt, cmd_override=tc_cmd)
    test_result = _run_tests(wt, cmd_override=test_cmd)

    all_skipped = (lint_result.get("skipped") and typecheck_result.get("skipped")
                   and test_result.get("skipped"))
    if all_skipped:
        overall = "skipped"
    elif test_result["ok"]:
        overall = "passed"
    else:
        overall = "failed"

    return {"ok": overall != "failed", "data": {
        "overall": overall,
        "worktree": str(wt),
        "lint": {"passed": lint_result["passed"], "output": (lint_result.get("output") or "")[:TEST_OUTPUT_MAX]},
        "typecheck": {"passed": typecheck_result["passed"], "output": (typecheck_result.get("output") or "")[:TEST_OUTPUT_MAX]},
        "test": {"passed": test_result.get("ok"), "output": (test_result.get("output") or "")[:TEST_OUTPUT_MAX]},
    }}


def cmd_repos(_args) -> dict:
    """List configured repos and workspaces."""
    cfg = ConfigManager()
    return cfg.list_all()


def cmd_combined_status(args) -> dict:
    """Unified status: no repo → list repos; with repo → full session + feature status.

    Merges cmd_repos + cmd_status + cmd_progress into one tool.
    Agent-facing replacement for the three separate tools.
    """
    repo_name = getattr(args, "repo", None) or None  # treat "" same as None

    if not repo_name:
        # No repo specified → list available repos
        cfg = ConfigManager()
        repos_result = cfg.list_all()
        return {
            "ok": True,
            "data": {
                "mode": "list",
                "repos": repos_result.get("data", repos_result),
                "hint": "Pass repo=<name> to see full session status.",
            },
        }

    # With repo → session status + progress combined
    status_result = cmd_status(args)
    progress_result = cmd_progress(args)

    combined = {
        "ok": status_result.get("ok", False),
        "data": {
            "mode": "detail",
            "repo": repo_name,
            "session": status_result.get("data", {}),
            "progress": progress_result.get("data", {}),
        },
    }
    # Surface any errors
    for r in (status_result, progress_result):
        if not r.get("ok"):
            combined["ok"] = False
            combined["error"] = r.get("error", "")
            break
    return combined


def cmd_change_summary(args) -> dict:
    """Generate a structured change summary with unified diff, worktree path, and commit info.

    Works with the current session's worktree. Useful for reporting code changes
    to the user in a reviewable format.
    """
    repo = _resolve_locked_repo(args)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    wt = _get_session_worktree(repo, lock) or repo

    base_ref = getattr(args, "base_ref", None) or lock.get("branch", "HEAD~1")
    summary = _build_change_summary(wt, base_ref)

    return {
        "ok": True,
        "data": {
            "change_summary": summary,
            "instruction": (
                "Present this change summary to the user. MUST include:\n"
                "1. The unified diff (in a code block)\n"
                "2. The worktree path so they can review locally\n"
                "3. The review command they can copy-paste\n"
                "4. The diff stat summary\n"
                "Do NOT rewrite the diff as before/after snippets — show the actual diff."
            ),
        },
    }


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
                fixes.append("cm unlock or cm start (re-joins and renews)")
            branch = lock.get("branch", "")
            if branch:
                branch_check = _run_git(repo, ["rev-parse", "--verify", branch], check=False)
                if branch_check.returncode != 0:
                    issues.append(f"lock references branch '{branch}' which does not exist")
                    fixes.append("cm unlock --force")
            # Check session_worktree health for write sessions
            if not lock.get("read_only", False):
                session_wt = lock.get("session_worktree", "")
                # Detect branches stored as session branch that can't be used in a worktree
                if branch:
                    checked_out_at = _find_checked_out_branch_path(repo, branch)
                    if checked_out_at and checked_out_at.resolve() == repo.resolve():
                        issues.append(
                            f"lock branch '{branch}' is checked out in the main repo — "
                            "cannot create a session worktree for it"
                        )
                        fixes.append(
                            "cm doctor --fix (will delete the stale lock; re-run _cm_next to restart)"
                        )
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
    lock_path = repo / CM_DIR / "lock.json"

    for issue in issues:
        if "cannot create a session worktree for it" in issue:
            # Stale lock with branch=main (or any branch already in main repo).
            # Clear the lock so the agent can re-lock without a branch.
            _atomic_json_update(lock_path, lambda d: (d.clear(), {"ok": True})[1])

        elif "orphaned worktree" in issue:
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

        elif "claims.json references Feature" in issue:
            _reconcile_plan_claims(repo)

        elif "expired" in issue:
            pass  # Don't auto-fix expired locks — user should decide

        elif "orphaned branch" in issue:
            branch_name = issue.split(": ", 1)[1] if ": " in issue else ""
            if branch_name:
                _run_git(repo, ["branch", "-D", branch_name], check=False)


DIFF_MAX_CHARS = 3000  # Truncate diff output to keep messages readable


def _build_change_summary(worktree: Path, base_ref: str = "HEAD~1") -> dict:
    """Build a structured change summary with unified diff, path, and commit info.

    Returns a dict with keys: worktree, commit, files_changed, diff, review_command.
    """
    head = _run_git(worktree, ["rev-parse", "--short", "HEAD"], check=False).stdout.strip()
    head_full = _run_git(worktree, ["rev-parse", "HEAD"], check=False).stdout.strip()
    commit_msg = _run_git(worktree, ["log", "-1", "--pretty=%s"], check=False).stdout.strip()

    # Get changed files
    files_out = _run_git(worktree, ["diff", "--name-only", f"{base_ref}..HEAD"], check=False).stdout.strip()
    files_changed = [f for f in files_out.splitlines() if f] if files_out else []

    # Get unified diff
    diff_out = _run_git(worktree, ["diff", f"{base_ref}..HEAD"], check=False).stdout
    if len(diff_out) > DIFF_MAX_CHARS:
        diff_out = diff_out[:DIFF_MAX_CHARS] + f"\n... (truncated, full diff: {len(diff_out)} chars)"

    # Get stat summary
    stat_out = _run_git(worktree, ["diff", "--stat", f"{base_ref}..HEAD"], check=False).stdout.strip()

    # Build GitHub compare URL if possible
    diff_url = None
    try:
        resolved_base = _run_git(worktree, ["rev-parse", "--verify", base_ref], check=False).stdout.strip()
        remote_url = _run_git(worktree, ["remote", "get-url", "origin"], check=False).stdout.strip()
        if remote_url and resolved_base and head_full:
            # Normalize to https URL: git@github.com:owner/repo.git → https://github.com/owner/repo
            if remote_url.startswith("git@"):
                remote_url = remote_url.replace(":", "/", 1).replace("git@", "https://", 1)
            repo_url = remote_url.removesuffix(".git")
            diff_url = f"{repo_url}/compare/{resolved_base}...{head_full}"
    except Exception:
        pass

    return {
        "worktree": str(worktree),
        "commit": head,
        "commit_full": head_full,
        "commit_message": commit_msg,
        "files_changed": files_changed,
        "diff_stat": stat_out,
        "diff": diff_out,
        "diff_url": diff_url,
        "review_command": f"cd {worktree} && git diff {base_ref}..HEAD",
    }


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


# ══════════════════════════════════════════════════════════
#  File Operations (v4.5)
# ══════════════════════════════════════════════════════════


def _resolve_working_dir(repo: Path, args) -> Path:
    """Resolve working directory: explicit feature > in-progress feature > session worktree > repo root.

    When no feature is specified, auto-detects any feature in 'developing' phase
    and uses its worktree. This prevents edits from landing in the session worktree
    when the agent omits the feature parameter.
    """
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json") or {}

    # If feature is specified and has a worktree, use it
    feature_id = getattr(args, "feature", None)
    if feature_id:
        feat = claims.get("features", {}).get(str(feature_id), {})
        wt = feat.get("worktree")
        if wt and Path(wt).exists():
            return Path(wt)

    # Auto-detect: if exactly one feature is in 'developing' phase, use its worktree
    if not feature_id:
        developing = [
            f for f in claims.get("features", {}).values()
            if f.get("phase") == "developing" and f.get("worktree")
        ]
        if len(developing) == 1:
            wt = developing[0]["worktree"]
            if Path(wt).exists():
                return Path(wt)

    # Otherwise use session worktree
    session_wt = lock.get("session_worktree")
    if session_wt and Path(session_wt).exists():
        return Path(session_wt)

    return repo


def _is_within_repo(target: Path, repo: Path) -> bool:
    """Check if target path is within repo or any of its worktrees."""
    resolved = target.resolve()
    repo_resolved = repo.resolve()
    # Allow repo itself
    if resolved.is_relative_to(repo_resolved):
        return True
    # Allow sibling worktree dirs (session and feature worktrees)
    # Must match exact naming convention: {repo}-session or {repo}-feature-{id}
    repo_parent = repo_resolved.parent
    repo_name = repo_resolved.name
    if resolved.is_relative_to(repo_parent):
        rel = resolved.relative_to(repo_parent)
        first_part = str(rel.parts[0]) if rel.parts else ""
        if (first_part == f"{repo_name}-session"
                or first_part.startswith(f"{repo_name}-feature-")):
            return True
    return False


def cmd_read(args) -> dict:
    """Read file contents with optional line range.

    Available in all modes. Auto-resolves paths relative to session/feature worktree.
    """
    args.require_lock = False
    repo = _resolve_locked_repo(args)
    raw_file = Path(args.file)
    target = _resolve_edit_target(repo, raw_file, args)

    if not _is_within_repo(target, repo):
        return {"ok": False, "error": f"path {target} is outside repo"}
    if not target.exists():
        return {"ok": False, "error": f"file not found: {target}"}
    if target.is_dir():
        return {"ok": False, "error": f"{target} is a directory, not a file"}

    lines = target.read_text(errors="replace").splitlines(keepends=True)
    start = max(1, getattr(args, "start_line", None) or 1)
    end = min(len(lines), getattr(args, "end_line", None) or len(lines))

    MAX_LINES = 2000
    if end - start + 1 > MAX_LINES:
        end = start + MAX_LINES - 1

    selected = lines[start - 1:end]
    numbered = "".join(f"{start + i:6d}\t{line}" for i, line in enumerate(selected))

    return {"ok": True, "data": {
        "file": str(target),
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "content": numbered,
    }}


def cmd_find(args) -> dict:
    """Find files by glob pattern.

    Available in all modes. Searches relative to session/feature worktree.
    """
    args.require_lock = False
    repo = _resolve_locked_repo(args)
    cwd = _resolve_working_dir(repo, args)
    pattern = args.pattern
    max_results = getattr(args, "max_results", 50) or 50

    # Normalize absolute patterns to relative (Path.glob requires relative patterns)
    if pattern.startswith("/"):
        try:
            pattern = str(Path(pattern).relative_to(cwd))
        except ValueError:
            return {"ok": False, "error": (
                f"Pattern '{pattern}' is absolute and not under cwd '{cwd}'. "
                "Use a relative glob pattern like '**/*.py' or 'src/**/*.ts'."
            )}

    try:
        matches = sorted(cwd.glob(pattern))
    except ValueError as e:
        return {"ok": False, "error": f"Invalid glob pattern '{pattern}': {e}. Use relative patterns like '**/*.py'."}
    files = [str(m.relative_to(cwd)) for m in matches if m.is_file()
             and ".git" not in m.relative_to(cwd).parts]

    truncated = len(files) > max_results
    files = files[:max_results]

    return {"ok": True, "data": {
        "pattern": pattern,
        "cwd": str(cwd),
        "files": files,
        "count": len(files),
        "truncated": truncated,
    }}


def cmd_grep(args) -> dict:
    """Search file contents by regex pattern.

    Available in all modes. Searches relative to session/feature worktree.
    Uses ripgrep if available, falls back to grep.
    """
    args.require_lock = False
    repo = _resolve_locked_repo(args)
    cwd = _resolve_working_dir(repo, args)
    pattern = args.pattern
    file_glob = getattr(args, "glob", None)
    context = getattr(args, "context", 2) or 2
    max_results = getattr(args, "max_results", 20) or 20

    rg = shutil.which("rg")
    cmd = [rg] if rg else ["grep"]
    if rg:
        cmd += ["-n", f"-C{context}",
                "--max-filesize=1M", "--no-heading",
                f"--max-count={max_results}"]
        if file_glob:
            cmd += ["--glob", file_glob]
        cmd += [pattern, str(cwd)]
    else:
        cmd += ["-rn", f"-C{context}", pattern, str(cwd)]
        if file_glob:
            cmd += ["--include", file_glob]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"ok": False, "error": f"grep failed: {exc}"}

    MAX_OUTPUT = 10000
    truncated = len(output) > MAX_OUTPUT
    if truncated:
        output = output[:MAX_OUTPUT] + "\n...(output truncated)..."

    return {"ok": True, "data": {
        "pattern": pattern,
        "cwd": str(cwd),
        "output": output,
        "truncated": truncated,
    }}


def _resolve_edit_target(repo: Path, raw_file: Path, args) -> Path:
    """Resolve an edit target path. Two clear rules:

    1. If the path references .coding-master/ (in any position), resolve against repo root.
       CM metadata always lives in repo root, never in worktrees.
    2. Otherwise, resolve relative to the feature/session worktree via _resolve_working_dir.
    """
    cm_dir = (repo / CM_DIR).resolve()

    # Rule 1: anything referencing CM_DIR → resolve against repo root
    if not raw_file.is_absolute():
        # Extract the CM-relative portion if .coding-master/ appears anywhere in the path
        parts = list(raw_file.parts)
        if CM_DIR in parts:
            cm_idx = parts.index(CM_DIR)
            return (repo / Path(*parts[cm_idx:])).resolve()
        # Bare .md filename (e.g. "PLAN.md", "report.md") → prefer .coding-master/
        # because CM metadata always lives there, even if the file doesn't exist yet.
        if raw_file.suffix == ".md" and len(parts) == 1:
            cwd = _resolve_working_dir(repo, args)
            cwd_candidate = (cwd / raw_file).resolve()
            cm_candidate = (repo / CM_DIR / raw_file).resolve()
            # Prefer CM dir unless the file only exists in the worktree
            if cm_candidate.exists() or not cwd_candidate.exists():
                return cm_candidate

    # Rule 2: absolute paths
    if raw_file.is_absolute():
        resolved = raw_file.resolve()
        # Absolute path to a .md file that doesn't exist (e.g. /path/worktree/PLAN.md)?
        # Redirect to .coding-master/ in repo root — CM metadata never lives in worktrees.
        if not resolved.exists() and resolved.suffix == ".md":
            cm_candidate = (repo / CM_DIR / resolved.name).resolve()
            if str(cm_candidate).startswith(str((repo / CM_DIR).resolve())):
                return cm_candidate
        return resolved

    cwd = _resolve_working_dir(repo, args)
    return (cwd / raw_file).resolve()


def cmd_edit(args) -> dict:
    """Edit file by exact text replacement. Only available in deliver/debug modes.

    old_text must match exactly once in the file for safety.
    Requires a feature in 'developing' phase (deliver mode) or an active debug session.
    """
    repo = _resolve_locked_repo(args)
    mode = _get_session_mode(repo)
    if mode in READ_ONLY_MODES:
        return {"ok": False, "error": f"edit not allowed in {mode} mode (read-only)"}

    cm_dir = (repo / CM_DIR).resolve()
    raw_file = Path(args.file)
    target = _resolve_edit_target(repo, raw_file, args)

    is_cm_metadata = (
        str(target).startswith(str(cm_dir))
        and target.suffix == ".md"
    )

    if not _is_within_repo(target, repo):
        return {"ok": False, "error": f"path {target} is outside repo"}

    # Require a feature in 'developing' phase for deliver mode (source code only)
    if mode == "deliver" and not is_cm_metadata:
        claims = _atomic_json_read(repo / CM_DIR / "claims.json")
        features = claims.get("features", {}) if claims else {}
        has_developing = any(
            f.get("phase") == "developing" for f in features.values()
        )
        if not has_developing:
            lock = _atomic_json_read(repo / CM_DIR / "lock.json")
            phase = lock.get("session_phase", "locked") if lock else "locked"
            if phase == "locked":
                return {"ok": False,
                        "error": "Cannot edit source code: no feature in 'developing' phase. Session is 'locked' (planning phase).",
                        "next_action": {"tool": "_cm_next", "args": {"repo": args.repo}},
                        "hint": ("Use _cm_next to advance the workflow automatically. "
                                 "_cm_next will guide you through: "
                                 "1) creating PLAN.md (use _cm_edit); "
                                 "2) auto-validating and claiming a feature; "
                                 "3) filling Analysis + Plan (use _cm_edit); "
                                 "4) advancing to 'developing' so _cm_edit can modify source code.")}
            elif phase == "reviewed":
                # Find first unclaimed feature
                plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
                first_feature = 1
                for fid_str, f in features.items():
                    if f.get("phase") in (None, "unclaimed"):
                        first_feature = int(fid_str)
                        break
                return {"ok": False,
                        "error": "Cannot edit source code: session is 'reviewed' but no feature is in 'developing' phase.",
                        "next_action": {"tool": "_cm_next", "args": {"repo": args.repo}},
                        "hint": f"Call _cm_next — it will auto-claim feature {first_feature} and advance it to 'developing'."}
            else:
                return {"ok": False,
                        "error": f"Cannot edit source code: no feature in 'developing' phase. Session is '{phase}'.",
                        "next_action": {"tool": "_cm_next", "args": {"repo": args.repo}},
                        "hint": "Call _cm_next — it will claim a feature and advance it to 'developing'."}

    # For CM metadata: old_text="" means create/overwrite (PLAN.md is "write once per session")
    if is_cm_metadata and args.old_text == "":
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            created = not target.exists()
            target.write_text(args.new_text)
        except OSError as e:
            return {"ok": False, "error": f"file write failed: {e}"}
        result = {"ok": True, "data": {"file": str(target), "replacements": 1, "created": created}}
        if target.name == "PLAN.md":
            orphans = _reconcile_plan_claims(repo)
            if orphans:
                result["data"]["reconciled_orphans"] = orphans
        return result

    if not target.exists():
        return {"ok": False, "error": f"file not found: {target}"}

    content = target.read_text()
    old_text = args.old_text
    new_text = args.new_text

    if old_text == new_text:
        return {"ok": False, "error": "old_text and new_text are identical"}

    count = content.count(old_text)
    if count == 0:
        return {"ok": False, "error": "old_text not found in file"}
    if count > 1:
        return {"ok": False, "error": f"old_text matches {count} locations; "
                "provide more context to make it unique"}

    new_content = content.replace(old_text, new_text, 1)
    try:
        target.write_text(new_content)
    except OSError as e:
        return {"ok": False, "error": f"file write failed: {e}"}

    result = {"ok": True, "data": {
        "file": str(target),
        "replacements": 1,
    }}
    if is_cm_metadata and target.name == "PLAN.md":
        orphans = _reconcile_plan_claims(repo)
        if orphans:
            result["data"]["reconciled_orphans"] = orphans
    return result


def cmd_start(args) -> dict:
    """One-shot session setup: lock + copy plan + plan-ready. Rolls back on failure."""
    repo = _repo_path(args.repo)
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
        return {
            "ok": False,
            "error": str(exc),
            "data": {"rolled_back": True},
            "hint": (
                "Session was rolled back (lock cleared). "
                "Call _cm_next(repo=...) — it will re-lock, then guide you to fix PLAN.md and advance."
            ),
        }


_PLAN_TEMPLATE = """\
# Feature Plan

## Origin Task
<describe what needs to be done>

## Features

### Feature 1: <short title>
**Depends on**: —

#### Task
<describe the specific work>

#### Acceptance Criteria
- [ ] <criterion one>
- [ ] <criterion two>

<!-- Only add more features if the task genuinely requires independent, parallel work streams.
     Most tasks need just 1 feature. Do NOT split analysis/scan into a separate feature —
     analysis is part of each feature's engine phase, not a standalone feature. -->
"""

_SCOPE_TEMPLATE = """\
Specify what to analyze. Pass as the `diff` or `files` argument to _cm_next(intent='scope'):
  diff  — e.g. "HEAD~5..HEAD" to review recent commits
  files — e.g. "src/foo.py,src/bar.py" to analyze specific files
"""


def _next_claimable_feature(plan: dict, claims: dict) -> str | None:
    """Return the first feature ID that can be claimed (pending + deps done)."""
    features = claims.get("features", {})
    for fid in _topo_sort(plan):
        phase = features.get(fid, {}).get("phase", "pending")
        if phase not in ("pending", None):
            continue
        deps = plan[fid].get("depends_on", [])
        if all(features.get(d, {}).get("phase") == "done" for d in deps):
            return fid
    return None


def _in_progress_feature(claims: dict, plan: dict | None = None) -> tuple[str, str] | None:
    """Return (feature_id, phase) for any feature currently in-progress.

    When *plan* is provided, features not present in the plan are skipped
    (they are orphaned claims left over from a mid-session PLAN.md edit).
    """
    for fid, feat in claims.get("features", {}).items():
        if feat.get("phase") in ("analyzing", "developing"):
            if plan is not None and fid not in plan:
                continue
            return fid, feat["phase"]
    return None


def _reconcile_plan_claims(repo: Path) -> list[str]:
    """Mark claims entries as skipped when their feature was removed from PLAN.md.

    Returns the list of orphaned feature IDs that were cleaned up.
    Called at cmd_next entry and after PLAN.md edits to keep the two data
    sources consistent.
    """
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    if not plan:
        return []  # empty/missing plan — don't reconcile to avoid wiping everything

    claims = _atomic_json_read(repo / CM_DIR / "claims.json") or {}
    features = claims.get("features", {})
    orphans = [fid for fid in features
               if fid not in plan and features[fid].get("phase") not in ("done", "skipped")]
    if not orphans:
        return []

    # Clean up worktrees for active orphans
    for fid in orphans:
        feat = features[fid]
        wt = feat.get("worktree")
        if wt and feat.get("phase") in ("analyzing", "developing"):
            _remove_worktree(repo, wt)

    # Mark orphans as skipped (preserves audit trail)
    def _mark_orphans(data):
        for fid in orphans:
            f = data.get("features", {}).get(fid)
            if f:
                f["phase"] = "skipped"
                f["skipped_reason"] = "removed_from_plan"
        return {"ok": True}
    _atomic_json_update(repo / CM_DIR / "claims.json", _mark_orphans)

    _append_journal(repo, "system", "reconcile",
                    f"Orphaned features cleaned: {orphans}")
    return orphans


def cmd_next(args) -> dict:
    """Auto-advance the workflow to the next creative breakpoint.

    Automatically runs all mechanical steps (lock, plan-ready, claim, dev,
    integrate, submit) and stops only when agent input is needed:
      write_plan      — create or fix PLAN.md
      write_analysis  — fill Analysis + Plan in a feature markdown
      write_code      — implement the feature
      fix_code        — fix failing tests
      fix_integration — fix integration test failures
      define_scope    — specify what to review/analyze/debug
      write_report    — write review/diagnosis report
      complete        — all done

    Typical usage:
      _cm_next(repo="myrepo")               # advance to next breakpoint
      _cm_next(repo="myrepo", intent="test") # run tests on current feature
      _cm_next(repo="myrepo", mode="review") # start a review session
    """
    repo = _repo_path(args.repo)
    intent = getattr(args, "intent", None)
    mode = getattr(args, "mode", None) or "deliver"

    # ── Repeated breakpoint detection ─────────────────────────────────────
    # If the agent calls _cm_next without changing state (no _cm_edit, no intent),
    # it will get the same breakpoint back. Detect and escalate.
    max_depth = getattr(args, "_depth", 0)
    if max_depth == 0 and not intent:
        lock_data = _atomic_json_read(repo / CM_DIR / "lock.json") or {}
        last_bp = lock_data.get("_last_breakpoint")
        # Will be checked after result is computed (see end of function)

    if max_depth > 25:
        lock = _atomic_json_read(repo / CM_DIR / "lock.json")
        return {
            "ok": False,
            "error": "cmd_next: max auto-advance depth reached — possible loop detected",
            "state": lock,
            "hint": "Run _cm_doctor to check for state inconsistencies.",
        }

    def _recurse(**extra):
        """Advance one more step (depth-tracked to prevent loops)."""
        import copy as _copy
        next_args = _copy.copy(args)
        next_args._depth = max_depth + 1
        next_args.intent = None  # consumed
        for k, v in extra.items():
            setattr(next_args, k, v)
        return cmd_next(next_args)

    # ── Read current lock state ──────────────────────────────────────────────
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    session_phase = lock.get("session_phase") if lock else None
    locked_mode = lock.get("mode", "deliver") if lock else mode

    # ── No active session → auto lock ────────────────────────────────────────
    if not lock or not session_phase or session_phase == "done":
        lock_args = copy.copy(args)
        lock_args.mode = mode
        lock_result = cmd_lock(lock_args)
        if not lock_result.get("ok"):
            return lock_result
        return _recurse()

    # ── Mode conflict: requested mode differs from locked session mode ────────
    requested_mode = getattr(args, "mode", None)
    force = getattr(args, "force", False)
    if requested_mode and requested_mode != locked_mode:
        phase = lock.get("session_phase", "?")
        # Auto-switch if no real work in progress; require force only when
        # there are claimed features or the session has advanced past planning.
        has_work = phase in ("working", "integrating")
        if force or not has_work:
            unlock_args = copy.copy(args)
            unlock_args.force = True
            cmd_unlock(unlock_args)
            return _recurse()
        return {
            "ok": False,
            "breakpoint": "mode_conflict",
            "instruction": (
                f"Session has in-progress work (phase: {phase}, mode: {locked_mode}). "
                f"Switching to '{requested_mode}' will discard it. "
                f"Call _cm_next(repo='{args.repo}', mode='{requested_mode}', force=True) to confirm."
            ),
            "context": {
                "current_mode": locked_mode,
                "requested_mode": requested_mode,
                "session_phase": phase,
                "branch": lock.get("branch"),
            },
        }

    # ── Route by mode ────────────────────────────────────────────────────────
    if locked_mode != "deliver":
        result = _cmd_next_review(repo, lock, args, locked_mode, intent, _recurse)
    else:
        result = _cmd_next_deliver(repo, lock, args, intent, _recurse)

    # ── Repeated breakpoint guard ─────────────────────────────────────────
    # If this is a top-level call (not a _recurse) without intent, check if
    # we're returning the same breakpoint as last time. If so, the agent
    # called _cm_next without doing any work — escalate the instruction.
    bp = result.get("breakpoint")
    feat = result.get("feature")
    if bp and max_depth == 0:
        bp_key = f"{bp}:{feat}" if feat else bp
        lock_data = _atomic_json_read(repo / CM_DIR / "lock.json") or {}
        last_bp = lock_data.get("_last_breakpoint")
        repeat_count = lock_data.get("_bp_repeat_count", 0)

        if last_bp == bp_key and not intent:
            repeat_count += 1
            if repeat_count >= 2:
                # Hard block — return error to break the loop
                if bp == "review_changes":
                    msg = (
                        f"STOP — you have called _cm_next without intent {repeat_count + 1} times at "
                        f"'review_changes'. You MUST pass intent based on user's decision: "
                        f"intent='confirm' / intent='fix' / intent='abort'."
                    )
                else:
                    msg = (
                        f"STOP calling _cm_next — you have received '{bp}' {repeat_count + 1} times "
                        f"without making changes. You MUST use _cm_edit to do the work described "
                        f"in 'instruction' BEFORE calling _cm_next again."
                    )
                result["ok"] = False
                result["error"] = msg
                result["instruction"] = (
                    f"⚠ REPEATED BREAKPOINT ({repeat_count + 1}x). "
                    + result.get("instruction", "")
                )
        else:
            repeat_count = 0

        def _update_bp_tracking(data):
            data["_last_breakpoint"] = bp_key
            data["_bp_repeat_count"] = repeat_count
            return {"ok": True}
        _atomic_json_update(repo / CM_DIR / "lock.json", _update_bp_tracking)

    return result


def _cmd_next_deliver(repo: Path, lock: dict, args, intent, _recurse) -> dict:
    """Deliver-mode breakpoint logic."""
    phase = lock.get("session_phase", "locked")
    plan_path = repo / CM_DIR / "PLAN.md"
    claims_path = repo / CM_DIR / "claims.json"

    # ── locked phase ─────────────────────────────────────────────────────────
    if phase == "locked":
        plan = _parse_plan_md(plan_path)
        if not plan:
            return {
                "ok": True,
                "breakpoint": "write_plan",
                "instruction": (
                    "Create PLAN.md at '.coding-master/PLAN.md' using _cm_edit "
                    "(old_text='', new_text=<your plan>). "
                    "IMPORTANT: Keep it minimal — most tasks need only 1 feature. "
                    "Do NOT split scanning/analysis into a separate feature (that is the engine's job). "
                    "Only create multiple features for genuinely independent work streams. "
                    "Then call _cm_next again — it will auto-validate and advance."
                ),
                "template": _PLAN_TEMPLATE,
                "edit_target": ".coding-master/PLAN.md",
            }

        # PLAN.md exists → try plan-ready
        ready_result = cmd_plan_ready(args)
        if not ready_result.get("ok"):
            return {
                "ok": False,
                "breakpoint": "fix_plan",
                "error": ready_result.get("error", "plan-ready failed"),
                "instruction": (
                    "Fix PLAN.md using _cm_edit, then call _cm_next again."
                ),
                "edit_target": ".coding-master/PLAN.md",
            }
        # plan-ready advanced to "reviewed" → recurse
        return _recurse()

    # ── reviewed / working phase ─────────────────────────────────────────────
    if phase in ("reviewed", "working"):
        claims = _atomic_json_read(claims_path) or {}
        plan = _parse_plan_md(plan_path)

        # ── Reconcile: clean orphaned claims for features deleted from PLAN.md ──
        orphans = _reconcile_plan_claims(repo)
        if orphans:
            claims = _atomic_json_read(claims_path) or {}  # re-read after mutation

        # Handle skip_feature intent: mark a feature as skipped so it's excluded
        if intent == "skip_feature":
            fid_to_skip = str(getattr(args, "feature", "") or "")
            if not fid_to_skip:
                return {"ok": False, "error": "pass feature=N to identify which feature to skip"}
            feat = claims.get("features", {}).get(fid_to_skip, {})
            if not feat:
                return {"ok": False, "error": f"feature {fid_to_skip} not found in claims"}
            wt = feat.get("worktree", "")

            def _mark_skipped(data):
                f = data.get("features", {}).get(fid_to_skip, {})
                f["phase"] = "skipped"
                f["skipped_at"] = datetime.now(timezone.utc).isoformat()
                return {"ok": True}
            _atomic_json_update(repo / CM_DIR / "claims.json", _mark_skipped)
            if wt:
                _remove_worktree(repo, wt)
            agent = _resolve_agent(args)
            _append_journal(repo, agent, "skip_feature", f"Feature {fid_to_skip} skipped by user request")
            return _recurse()

        # Check for any in-progress feature first
        in_progress = _in_progress_feature(claims, plan)
        if in_progress:
            fid, feat_phase = in_progress
            spec = plan.get(fid, {})
            feat_md = _find_feature_md(repo, fid)

            if feat_phase == "analyzing":
                has_analysis, has_plan = _check_feature_md_sections(feat_md)
                if not has_analysis or not has_plan:
                    # ── ENGINE: analyze (read code, write Analysis+Plan) ──
                    retries = _get_engine_retry_count(repo, fid, "analyze")
                    if retries >= MAX_ENGINE_RETRIES:
                        return {
                            "ok": False,
                            "breakpoint": "engine_failed",
                            "feature": int(fid),
                            "phase": "analyze",
                            "instruction": (
                                f"Engine failed to analyze Feature {fid} after {MAX_ENGINE_RETRIES} attempts. "
                                "Write ## Analysis and ## Plan sections manually via _cm_edit, "
                                "then call _cm_next again."
                            ),
                            "feature_md": str(feat_md) if feat_md else "",
                        }
                    engine_result = _run_engine_for_feature(repo, fid, "analyze", args)
                    # Always re-check sections — engine may report ok=True but not write them
                    has_analysis, has_plan = _check_feature_md_sections(feat_md)
                    if not has_analysis or not has_plan:
                        _increment_engine_retry(repo, fid, "analyze")
                        return _recurse()  # retry
                    _reset_engine_retries(repo, fid)
                # Analysis + Plan filled → auto dev
                dev_result_args = copy.copy(args)
                dev_result_args.feature = int(fid)
                dev_result = cmd_dev(dev_result_args)
                if not dev_result.get("ok"):
                    return dev_result
                return _recurse()

            if feat_phase == "developing":
                feat_data = claims.get("features", {}).get(fid, {})
                dev_state = feat_data.get("developing", {})
                test_status = dev_state.get("test_status")
                wt = Path(feat_data.get("worktree") or str(repo))

                # ── Test intent: auto-commit + run tests ──
                if intent == "test":
                    _auto_commit(wt)
                    test_args = copy.copy(args)
                    test_args.feature = int(fid)
                    test_result = cmd_test(test_args)
                    if not test_result.get("ok"):
                        return test_result

                    # Re-read claims after test
                    claims = _atomic_json_read(claims_path) or {}
                    dev_state = claims.get("features", {}).get(fid, {}).get("developing", {})
                    test_status = dev_state.get("test_status")

                    if test_status == "passed":
                        _reset_engine_retries(repo, fid)
                        done_args = copy.copy(args)
                        done_args.feature = int(fid)
                        done_result = cmd_done(done_args)
                        if not done_result.get("ok"):
                            return done_result
                        return _recurse()
                    # Tests failed → fall through to fix below

                # ── ENGINE: implement or fix ──
                if test_status == "failed":
                    engine_phase = "fix"
                    test_output = dev_state.get("test_output", "")
                else:
                    engine_phase = "implement"
                    test_output = ""

                retries = _get_engine_retry_count(repo, fid, engine_phase)
                if retries >= MAX_ENGINE_RETRIES:
                    return {
                        "ok": False,
                        "breakpoint": "engine_failed",
                        "feature": int(fid),
                        "phase": engine_phase,
                        "worktree": str(wt),
                        "test_output": test_output,
                        "instruction": (
                            f"Engine failed to {engine_phase} Feature {fid} after "
                            f"{MAX_ENGINE_RETRIES} attempts. "
                            "Fix manually via _cm_edit, then call _cm_next again."
                        ),
                    }

                engine_result = _run_engine_for_feature(
                    repo, fid, engine_phase, args, test_output=test_output)
                if not engine_result.get("ok"):
                    _increment_engine_retry(repo, fid, engine_phase)
                else:
                    # Reset retries on success, but still need to test
                    if engine_phase == "implement":
                        _reset_engine_retries(repo, fid)

                # Auto-commit engine's changes + auto-test
                _auto_commit(wt)
                return _recurse(intent="test")

        # No in-progress feature → find next claimable
        next_fid = _next_claimable_feature(plan, claims)
        if next_fid:
            claim_args = copy.copy(args)
            claim_args.feature = int(next_fid)
            claim_result = cmd_claim(claim_args)
            if not claim_result.get("ok"):
                return claim_result
            return _recurse()

        # All features done or skipped → auto integrate
        _TERMINAL_PHASES = ("done", "skipped")
        all_done = all(
            claims.get("features", {}).get(fid, {}).get("phase") in _TERMINAL_PHASES
            for fid in plan
        )
        if not all_done:
            pending = [
                fid for fid in plan
                if claims.get("features", {}).get(fid, {}).get("phase") not in _TERMINAL_PHASES
            ]
            return {
                "ok": False,
                "breakpoint": "blocked",
                "instruction": (
                    f"Features {pending} are blocked on unfinished dependencies. "
                    "To skip a feature: _cm_next(intent='skip_feature', feature=N). "
                    "Check _cm_status for details."
                ),
            }

        int_result = cmd_integrate(args)
        if not int_result.get("ok"):
            # ── ENGINE: fix integration failure ──
            retries = _get_engine_retry_count(repo, "integration", "fix")
            if retries >= MAX_ENGINE_RETRIES:
                failing = int_result.get("data", {}).get("failed_features", [])
                return {
                    "ok": False,
                    "breakpoint": "engine_failed",
                    "phase": "fix_integration",
                    "failed_features": failing,
                    "error": int_result.get("error", "integration failed"),
                    "instruction": (
                        f"Engine failed to fix integration after {MAX_ENGINE_RETRIES} attempts. "
                        "Fix manually via _cm_edit, then call _cm_next again."
                    ),
                }
            session_wt = lock.get("session_worktree", "")
            engine_result = _run_engine_for_feature(
                repo, "integration", "fix", args,
                test_output=int_result.get("error", ""),
                worktree_override=Path(session_wt) if session_wt else None,
            )
            if not engine_result.get("ok"):
                _increment_engine_retry(repo, "integration", "fix")
            if session_wt:
                _auto_commit(Path(session_wt))
            return _recurse()  # retry integrate

        # Integrate succeeded → transition to "reviewing" for diff review
        # Clear diff_shown so the fresh diff is presented on first review
        _atomic_json_update(repo / CM_DIR / "lock.json", lambda d: (
            d.update({"session_phase": "reviewing", "_review_diff_shown": False}), {"ok": True},
        )[1])
        return _recurse()

    # ── reviewing phase → diff review before submit ───────────────────────────
    if phase == "reviewing":
        intent = getattr(args, "intent", None)
        session_wt_str = lock.get("session_worktree", "")

        if intent == "confirm":
            # User approved → transition to "integrating" → recurse → auto submit
            _atomic_json_update(repo / CM_DIR / "lock.json", lambda d: (
                d.update({"session_phase": "integrating"}), {"ok": True},
            )[1])
            return _recurse()

        if intent == "fix":
            feedback = getattr(args, "feedback", "") or getattr(args, "message", "")
            if not session_wt_str:
                return {"ok": False, "error": "session worktree not found"}
            session_wt = Path(session_wt_str)
            engine_result = _run_engine_for_feature(
                repo, "session", "fix", args,
                test_output=f"User review feedback: {feedback}",
                worktree_override=session_wt,
            )
            if not engine_result.get("ok"):
                return {
                    "ok": False,
                    "breakpoint": "engine_failed",
                    "phase": "review_fix",
                    "error": engine_result.get("error", "engine fix failed"),
                    "instruction": (
                        "Engine failed to apply the fix. "
                        "Fix manually via _cm_edit, then call _cm_next(intent='confirm') to submit "
                        "or _cm_next(intent='abort') to discard."
                    ),
                }
            _auto_commit(session_wt, "fix: user-requested change before submit")
            # Re-run tests in session worktree
            test_result = _run_tests(session_wt)
            if not test_result.get("passed"):
                return {
                    "ok": False,
                    "breakpoint": "engine_failed",
                    "phase": "review_fix_test",
                    "error": test_result.get("output", "tests failed after fix"),
                    "instruction": (
                        "Fix applied but tests are failing. "
                        "Call _cm_next(intent='fix', feedback='...') to retry, "
                        "or _cm_next(intent='confirm') to submit anyway."
                    ),
                }
            # Back to review_changes with updated diff — reset diff_shown so new diff is shown
            _atomic_json_update(repo / CM_DIR / "lock.json",
                                lambda d: (d.update({"_review_diff_shown": False}), {"ok": True})[1])
            return _recurse()

        if intent == "abort":
            branch = lock.get("branch", "unknown")
            # Set phase to "done" so cmd_unlock doesn't refuse; worktree will be cleaned up
            _atomic_json_update(repo / CM_DIR / "lock.json", lambda d: (
                d.update({"session_phase": "done"}), {"ok": True},
            )[1])
            cmd_unlock(args)
            return {
                "ok": True,
                "breakpoint": "complete",
                "pr_url": "",
                "instruction": (
                    f"STOP — 不要再调用任何工具（包括 _cm_status）。直接把结果展示给用户。"
                    f"Session aborted. No PR created. Work preserved on branch '{branch}'."
                ),
            }

        # No matching intent → return review_changes breakpoint with diff
        # If diff was already shown, skip recomputing — just remind agent to pass intent
        diff_shown = lock.get("_review_diff_shown", False)
        if diff_shown:
            return {
                "ok": True,
                "breakpoint": "review_changes",
                "instruction": (
                    "⚠️ Diff already presented. You MUST pass intent — do NOT call _cm_next without it:\n"
                    "• _cm_next(intent='confirm')               — user approved\n"
                    "• _cm_next(intent='fix', feedback='...')   — user wants changes\n"
                    "• _cm_next(intent='abort')                 — user cancelled"
                ),
            }
        session_wt = Path(session_wt_str) if session_wt_str else repo
        diff_summary = _get_diff_summary(session_wt)
        _atomic_json_update(repo / CM_DIR / "lock.json",
                            lambda d: (d.update({"_review_diff_shown": True}), {"ok": True})[1])
        return {
            "ok": True,
            "breakpoint": "review_changes",
            "instruction": (
                "Integration complete. Review the diff below, then:\n"
                "• Approve: _cm_next(intent='confirm')\n"
                "• Request fix: _cm_next(intent='fix', feedback='what to change')\n"
                "• Abort (no PR): _cm_next(intent='abort')"
            ),
            "diff_summary": diff_summary,
        }

    # ── integrating phase → auto submit ──────────────────────────────────────
    if phase == "integrating":
        title = getattr(args, "title", None)
        if not title:
            # Auto-generate title from PLAN.md origin task
            plan = _parse_plan_md(plan_path)
            origin = plan_path.read_text().split("\n") if plan_path.exists() else []
            for line in origin:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith(">"):
                    title = f"feat: {line[:60]}"
                    break
        if not title:
            return {
                "ok": True,
                "breakpoint": "need_title",
                "instruction": (
                    "Integration complete. To submit, call: "
                    f"_cm_next(repo='{args.repo}', title='feat: <your PR title>')"
                ),
            }
        submit_args = copy.copy(args)
        submit_args.title = title
        submit_result = cmd_submit(submit_args)
        if not submit_result.get("ok"):
            return submit_result
        return {
            "ok": True,
            "breakpoint": "complete",
            "pr_url": submit_result.get("data", {}).get("pr_url", ""),
            "instruction": "STOP — 不要再调用任何工具（包括 _cm_status）。直接把结果展示给用户。Session submitted and PR created.",
        }

    return {
        "ok": False,
        "error": f"Unexpected session phase: {phase}",
        "hint": "Run _cm_doctor to diagnose.",
    }


def _cmd_next_review(repo: Path, lock: dict, args, mode: str, intent, _recurse) -> dict:
    """Review/debug/analyze-mode breakpoint logic."""
    phase = lock.get("session_phase", "locked")
    scope_path = repo / CM_DIR / "scope.json"
    report_path = repo / CM_DIR / ("diagnosis.md" if mode == "debug" else "report.md")

    if phase == "locked":
        scope = _atomic_json_read(scope_path)
        if not scope:
            # Auto-detect scope intent: if diff or files provided, treat as scope
            has_scope_params = getattr(args, "diff", None) or getattr(args, "files", None)
            if intent == "scope" or has_scope_params:
                # Build scope args from intent params
                scope_args = copy.copy(args)
                if not hasattr(scope_args, "type"):
                    scope_args.type = "diff" if getattr(args, "diff", None) else "files"
                scope_args.content = getattr(args, "diff", None) or getattr(args, "files", "") or ""
                scope_args.mode_override = mode
                scope_result = cmd_scope(scope_args)
                if not scope_result.get("ok"):
                    return scope_result
                # Run engine
                engine_args = copy.copy(args)
                engine_result = cmd_engine_run(engine_args)
                if not engine_result.get("ok"):
                    return engine_result
                findings = engine_result.get("data", {}).get("summary", "")
                _atomic_json_update(repo / CM_DIR / "lock.json",
                                    lambda d: (d.update({"_engine_findings": findings}), {"ok": True})[1])
                report_file = f".coding-master/{'diagnosis.md' if mode == 'debug' else 'report.md'}"
                return {
                    "ok": True,
                    "breakpoint": "write_report",
                    "instruction": (
                        f"STOP — do NOT call _cm_next again. "
                        f"Engine analysis complete. Write your {'diagnosis' if mode == 'debug' else 'report'} with: "
                        f"_cm_edit(repo='{args.repo}', file='{report_file}', old_text='', new_text='<your report>'). "
                        "Only after writing the file, call _cm_next to complete."
                    ),
                    "findings": findings,
                    "report_target": report_file,
                }

            return {
                "ok": True,
                "breakpoint": "define_scope",
                "instruction": (
                    f"Define what to {mode}. Call _cm_next with either:\n"
                    f"  _cm_next(repo='{args.repo}', diff='HEAD~5..HEAD')  — analyze recent commits\n"
                    f"  _cm_next(repo='{args.repo}', files='src/foo.py')   — analyze specific files"
                ),
                "hint": _SCOPE_TEMPLATE,
            }

        # Scope exists but no report yet
        if not report_path.exists():
            # Run engine only if not already done (check engine_done flag in lock)
            findings = lock.get("_engine_findings", "")
            if not findings:
                engine_args = copy.copy(args)
                engine_result = cmd_engine_run(engine_args)
                if engine_result.get("ok"):
                    findings = engine_result.get("data", {}).get("summary", "")
                    # Cache findings in lock so engine doesn't re-run on next _cm_next
                    _atomic_json_update(repo / CM_DIR / "lock.json",
                                        lambda d: (d.update({"_engine_findings": findings}), {"ok": True})[1])
            report_file = f".coding-master/{'diagnosis.md' if mode == 'debug' else 'report.md'}"
            return {
                "ok": True,
                "breakpoint": "write_report",
                "instruction": (
                    f"STOP — do NOT call _cm_next again. "
                    f"First write your {'diagnosis' if mode == 'debug' else 'report'} with: "
                    f"_cm_edit(repo='{args.repo}', file='{report_file}', old_text='', new_text='<your report>'). "
                    "Only after writing the file, call _cm_next to complete."
                ),
                "findings": findings,
                "report_target": report_file,
            }

        # Report exists → auto unlock
        unlock_args = copy.copy(args)
        unlock_args.force = False
        unlock_result = cmd_unlock(unlock_args)
        if not unlock_result.get("ok"):
            return unlock_result
        return {
            "ok": True,
            "breakpoint": "complete",
            "instruction": f"STOP — do NOT call _cm_next again. {mode.capitalize()} session complete. Report saved.",
            "report": str(report_path),
        }

    return {
        "ok": False,
        "error": f"Unexpected session phase '{phase}' for {mode} mode.",
        "hint": "Run _cm_doctor to diagnose.",
    }


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

    # regression
    _add_global_args(sub.add_parser("regression", help="Full regression (lint + typecheck + tests)"))

    # change-summary
    p_cs = sub.add_parser("change-summary", help="Generate change summary with diff")
    _add_global_args(p_cs)
    p_cs.add_argument("--base-ref", default=None, dest="base_ref",
                       help="Base ref for diff (default: session branch)")

    # doctor
    p_doctor = sub.add_parser("doctor", help="Diagnose + fix state")
    _add_global_args(p_doctor)
    p_doctor.add_argument("--fix", action="store_true")

    # read (v4.5)
    p_read = sub.add_parser("read", help="Read file contents")
    _add_global_args(p_read)
    p_read.add_argument("--file", required=True, help="File path")
    p_read.add_argument("--start-line", type=int, default=None, dest="start_line")
    p_read.add_argument("--end-line", type=int, default=None, dest="end_line")
    p_read.add_argument("--feature", "-f", type=int, default=None)

    # find (v4.5)
    p_find = sub.add_parser("find", help="Find files by glob pattern")
    _add_global_args(p_find)
    p_find.add_argument("--pattern", required=True, help="Glob pattern")
    p_find.add_argument("--max-results", type=int, default=50, dest="max_results")
    p_find.add_argument("--feature", "-f", type=int, default=None)

    # grep (v4.5)
    p_grep = sub.add_parser("grep", help="Search file contents")
    _add_global_args(p_grep)
    p_grep.add_argument("--pattern", required=True, help="Regex pattern")
    p_grep.add_argument("--glob", default=None, help="File filter glob")
    p_grep.add_argument("--context", type=int, default=2)
    p_grep.add_argument("--max-results", type=int, default=20, dest="max_results")
    p_grep.add_argument("--feature", "-f", type=int, default=None)

    # edit (v4.5)
    p_edit = sub.add_parser("edit", help="Edit file by text replacement")
    _add_global_args(p_edit)
    p_edit.add_argument("--file", required=True, help="File path")
    p_edit.add_argument("--old-text", required=True, dest="old_text")
    p_edit.add_argument("--new-text", required=True, dest="new_text")
    p_edit.add_argument("--feature", "-f", type=int, default=None)

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
        "regression": cmd_regression,
        "change-summary": cmd_change_summary,
        "doctor": cmd_doctor,
        "read": cmd_read,
        "find": cmd_find,
        "grep": cmd_grep,
        "edit": cmd_edit,
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
