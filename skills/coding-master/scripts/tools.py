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
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

# ── Add scripts dir to path so we can import siblings ──
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config_manager import ConfigManager

CM_DIR = ".coding-master"
EVIDENCE_DIR = "evidence"
LEASE_MINUTES = 120
TEST_OUTPUT_MAX = 500

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
        except json.JSONDecodeError:
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
        return {"ok": False, "error": f"lease expired at {lock.get('lease_expires_at')}. "
                "Run cm renew or cm doctor --fix"}
    return {"ok": True}


def _resolve_locked_repo(args) -> Path:
    """Resolve repo and verify lock exists."""
    repo = _repo_path(args.repo)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    if not lock:
        _fail("no active lock. Run cm lock first")
    return repo


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
    has_analysis = bool(re.search(
        r"^## Analysis\s*\n(.+)", text, re.MULTILINE | re.DOTALL
    ))
    has_plan = bool(re.search(
        r"^## Plan\s*\n(.+)", text, re.MULTILINE | re.DOTALL
    ))
    # More precise: check there's content between ## Analysis and next ##
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
    """Remove a git worktree, ignoring errors."""
    try:
        _run_git(repo, ["worktree", "remove", worktree_path, "--force"], check=False)
    except Exception:
        pass


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
        return {"ok": True, "output": "no test command detected (skipped)"}

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
        return {"passed": True, "command": None, "output": "no lint command detected (skipped)"}

    stdout, stderr, rc = _exec(str(cwd), lint_cmd)
    combined = stdout + stderr
    output = combined[-TEST_OUTPUT_MAX:] if len(combined) > TEST_OUTPUT_MAX else combined
    return {"passed": rc == 0, "command": lint_cmd, "output": output}


def _run_typecheck(cwd: Path) -> dict:
    """Run typecheck in the given directory. Returns {passed, command, output}."""
    from test_runner import _exec, _has_tool, _resolve_pytest_command

    tc_cmd = _resolve_typecheck_command(cwd)
    if not tc_cmd:
        return {"passed": True, "command": None, "output": "no typecheck command detected (skipped)"}

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
#  Commands
# ══════════════════════════════════════════════════════════


def cmd_lock(args) -> dict:
    """Lock workspace, create dev branch."""
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"

    # Verify clean working tree (exclude .coding-master/ artifacts and .gitignore)
    status = _run_git(repo, [
        "status", "--porcelain", "--", ".",
        ":(exclude).coding-master", ":(exclude).gitignore",
    ], check=False)
    if status.stdout.strip():
        return {"ok": False, "error": "working tree not clean, commit or stash first"}

    agent = _resolve_agent(args)
    reserved = {}

    def reserve_lock(data):
        if data and not _is_expired(data):
            return {"ok": False, "error": "already locked", "data": data}

        now = datetime.now(timezone.utc)
        branch = getattr(args, "branch", None) or f"dev/{args.repo}-{now.strftime('%m%d-%H%M')}"
        reserved.update({
            "repo": args.repo,
            "session_phase": "locked",
            "branch": branch,
            "locked_by": agent,
            "locked_at": now.isoformat(),
            "lease_expires_at": (now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
            "session_agents": [agent],
        })
        data.clear()
        data.update(reserved)
        return {"ok": True}

    result = _atomic_json_update(lock_path, reserve_lock)
    if not result.get("ok"):
        return result

    try:
        _run_git(repo, ["checkout", "-b", reserved["branch"]])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        _atomic_json_update(lock_path, lambda d: (d.clear(), {"ok": True})[1])
        err = getattr(exc, "stderr", "") or str(exc)
        return {"ok": False, "error": f"git checkout failed: {err}"}

    _ensure_gitignore(repo)
    _append_journal(repo, agent, "lock", f"Workspace locked, branch: {reserved['branch']}")
    return {"ok": True, "data": {"branch": reserved["branch"]}}


def cmd_unlock(args) -> dict:
    """Release workspace lock."""
    repo = _repo_path(args.repo)
    lock_path = repo / CM_DIR / "lock.json"

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

    agent = _resolve_agent(args)
    _append_journal(repo, agent, "plan-ready", f"PLAN.md reviewed: {len(plan)} features")
    return {"ok": True, "data": {"features": len(plan), "plan": list(plan.keys())}}


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

    return _atomic_json_update(claims_path, do_dev)


def cmd_test(args) -> dict:
    """Run lint+typecheck+tests, write evidence + claims.json."""
    repo = _resolve_locked_repo(args)
    claims_path = repo / CM_DIR / "claims.json"
    feature_id = str(args.feature)

    # Precondition check
    pre_err = _precondition_check(repo, feature_id)
    if pre_err:
        return pre_err

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
    overall = "passed" if (lint_result["passed"] and typecheck_result["passed"] and test_result["ok"]) else "failed"
    evidence = {
        "feature_id": feature_id,
        "created_at": now,
        "commit": head,
        "lint": {
            "passed": lint_result["passed"],
            "command": lint_result.get("command"),
            "output": (lint_result.get("output", "") or "")[:TEST_OUTPUT_MAX],
        },
        "typecheck": {
            "passed": typecheck_result["passed"],
            "command": typecheck_result.get("command"),
            "output": (typecheck_result.get("output", "") or "")[:TEST_OUTPUT_MAX],
        },
        "test": {
            "passed": test_result["ok"],
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
    response = {"ok": True, "data": {"worktree": worktree, "feature": feature_id, "phase": "developing"}}

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

    # Checkout dev branch, record pre-merge SHA for rollback
    _run_git(repo, ["checkout", branch])
    pre_merge_sha = _run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()

    # Build merge order and track results
    merge_order = _topo_sort(plan)
    merge_results = []

    for fid in merge_order:
        fb = claims["features"].get(fid, {}).get("branch")
        if not fb:
            continue
        merge_rc = subprocess.run(
            ["git", "merge", fb, "--no-edit"],
            cwd=repo, capture_output=True, text=True,
        )
        if merge_rc.returncode != 0:
            merge_results.append({"feature": fid, "branch": fb, "status": "conflict",
                                  "error": merge_rc.stderr.strip()})
            subprocess.run(["git", "merge", "--abort"], cwd=repo, capture_output=True)
            subprocess.run(
                ["git", "reset", "--hard", pre_merge_sha],
                cwd=repo, capture_output=True,
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
            commit = _run_git(repo, ["rev-parse", "HEAD"], check=False).stdout.strip()
            merge_results.append({"feature": fid, "branch": fb, "status": "merged", "commit": commit})

    # Run full tests on merged dev branch
    test_result = _run_tests(repo)
    output_summary = (test_result.get("output", "") or "")[:1000]

    if not test_result["ok"]:
        subprocess.run(
            ["git", "reset", "--hard", pre_merge_sha],
            cwd=repo, capture_output=True,
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
    return {"ok": True, "data": {"test_output": output_summary}}


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

    # Commit (idempotent)
    _run_git(repo, ["add", "-A", "--", ":(exclude).coding-master"], check=False)
    status_out = _run_git(repo, ["status", "--porcelain"], check=False).stdout.strip()
    if status_out:
        _run_git(repo, ["commit", "-m", args.title], check=False)

    # Push (idempotent)
    _run_git(repo, ["push", "-u", "origin", branch], check=False)

    # PR (idempotent)
    existing_pr = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url"],
        cwd=repo, capture_output=True, text=True,
    )
    pr_url = None
    if existing_pr.returncode != 0:
        pr_body = _generate_pr_body(repo)
        pr_result = subprocess.run(
            ["gh", "pr", "create", "--title", args.title, "--body", pr_body],
            cwd=repo, capture_output=True, text=True,
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
    else:
        try:
            pr_url = json.loads(existing_pr.stdout).get("url")
        except json.JSONDecodeError:
            pass

    # Cleanup worktrees (best effort)
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    for fid in plan:
        wt = claims.get("features", {}).get(fid, {}).get("worktree")
        if wt:
            _remove_worktree(repo, wt)

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


def cmd_progress(args) -> dict:
    """Read-only: show session + feature status + action guidance."""
    repo = _resolve_locked_repo(args)
    lock = _atomic_json_read(repo / CM_DIR / "lock.json")
    plan_path = repo / CM_DIR / "PLAN.md"
    plan = _parse_plan_md(plan_path)
    claims = _atomic_json_read(repo / CM_DIR / "claims.json")
    features_claims = claims.get("features", {})

    session_phase = lock.get("session_phase", "unknown")
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
            "action_steps": action_steps,
        })

    suggestions = _generate_suggestions(result, lock)

    agent = _resolve_agent(args)
    next_action = _compute_next_action(result, features_claims, lock, agent)
    session_next_action = _compute_session_next_action(result, features_claims, lock)

    return {"ok": True, "data": {
        "session_phase": session_phase,
        "session_steps": session_steps,
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


def _compute_next_action(
    features: list[dict], claims: dict, lock: dict, agent: str,
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
    features: list[dict], claims: dict, lock: dict,
) -> dict | None:
    """Compute the best next action for the session (global scope)."""
    session_phase = lock.get("session_phase", "unknown")

    if session_phase == "integrating":
        return {"command": "cm submit --title '...'", "reason": "Integration passed, ready to submit", "scope": "session"}

    # Any feature with failed/stale verification
    for f in features:
        fid = f["id"]
        claim = claims.get(fid, {})
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

    # 3. Orphaned worktrees
    expected_worktrees = set()
    if claims_path.exists():
        for feat in _atomic_json_read(claims_path).get("features", {}).values():
            if feat.get("worktree"):
                expected_worktrees.add(feat["worktree"])
    for d in repo.parent.iterdir():
        if d.name.startswith(f"{repo.name}-feature-") and str(d) not in expected_worktrees:
            issues.append(f"orphaned worktree: {d}")
            fixes.append(f"cm doctor --fix (will remove {d})")

    # 4. PLAN.md vs claims.json consistency
    plan = _parse_plan_md(repo / CM_DIR / "PLAN.md")
    if claims_path.exists():
        claims = _atomic_json_read(claims_path)
        for fid in claims.get("features", {}):
            if fid not in plan:
                issues.append(f"claims.json references Feature {fid} not in PLAN.md")

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

        elif "expired" in issue:
            pass  # Don't auto-fix expired locks — user should decide


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

    # Check no existing lock
    existing = _atomic_json_read(lock_path)
    if existing and not _is_expired(existing):
        return {"ok": False, "error": "already locked", "data": existing}

    # Step 1: Lock
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
                "plan": ready_result.get("data", {}),
                "rolled_back": False,
            }}
        else:
            # No plan yet — return locked state, user will create plan
            return {"ok": True, "data": {
                "branch": lock_result["data"]["branch"],
                "session_phase": "locked",
                "rolled_back": False,
            }}

    except Exception as exc:
        # Best-effort rollback
        if plan_created and plan_path.exists():
            try:
                plan_path.unlink()
            except OSError:
                pass
        cmd_unlock(args)
        return {"ok": False, "error": str(exc), "data": {"rolled_back": True}}


# ══════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(prog="cm", description="Coding Master v3")
    parser.add_argument("--repo", "-r", default=None, help="Target repo name")
    parser.add_argument("--agent", default=None, help="Agent identity")
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="One-shot: lock + plan + plan-ready")
    p_start.add_argument("--branch", default=None)
    p_start.add_argument("--plan-file", default=None, help="Path to PLAN.md to copy")

    # lock
    p_lock = sub.add_parser("lock", help="Lock workspace")
    p_lock.add_argument("--branch", default=None)

    # unlock
    sub.add_parser("unlock", help="Release lock")

    # status
    sub.add_parser("status", help="Show lock status")

    # renew
    sub.add_parser("renew", help="Renew lease")

    # plan-ready
    sub.add_parser("plan-ready", help="Validate PLAN.md")

    # claim
    p_claim = sub.add_parser("claim", help="Claim a feature")
    p_claim.add_argument("--feature", "-f", required=True, type=int)

    # dev
    p_dev = sub.add_parser("dev", help="Advance to developing")
    p_dev.add_argument("--feature", "-f", required=True, type=int)

    # test
    p_test = sub.add_parser("test", help="Run tests for feature")
    p_test.add_argument("--feature", "-f", required=True, type=int)

    # done
    p_done = sub.add_parser("done", help="Mark feature done")
    p_done.add_argument("--feature", "-f", required=True, type=int)

    # reopen
    p_reopen = sub.add_parser("reopen", help="Reopen done feature")
    p_reopen.add_argument("--feature", "-f", required=True, type=int)

    # integrate
    sub.add_parser("integrate", help="Merge + integration tests")

    # submit
    p_submit = sub.add_parser("submit", help="Push + PR + cleanup")
    p_submit.add_argument("--title", "-t", required=True)

    # progress
    sub.add_parser("progress", help="Show progress + action guidance")

    # journal
    p_journal = sub.add_parser("journal", help="Append to JOURNAL.md")
    p_journal.add_argument("--message", "-m", required=True)

    # doctor
    p_doctor = sub.add_parser("doctor", help="Diagnose + fix state")
    p_doctor.add_argument("--fix", action="store_true")

    args = parser.parse_args()

    # Auto-detect repo from cwd if not specified
    if not args.repo:
        cwd = Path.cwd()
        if (cwd / ".git").exists():
            args.repo = cwd.name
        else:
            _fail("--repo required (or run from within a git repo)")

    commands = {
        "start": cmd_start,
        "lock": cmd_lock,
        "unlock": cmd_unlock,
        "status": cmd_status,
        "renew": cmd_renew,
        "plan-ready": cmd_plan_ready,
        "claim": cmd_claim,
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
        result = handler(args)
        _output(result)
    except SystemExit:
        raise
    except Exception as exc:
        _output({"ok": False, "error": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
