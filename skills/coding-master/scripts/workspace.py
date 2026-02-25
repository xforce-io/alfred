#!/usr/bin/env python3
"""Workspace lock management and environment probing."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from config_manager import ConfigManager

LOCK_FILENAME = ".coding-master.lock"
ARTIFACT_DIR = ".coding-master"
DEFAULT_LEASE_MINUTES = 120  # 2 hours — generous for human-in-the-loop phases

GITIGNORE_ENTRIES = [".coding-master.lock", ".coding-master/"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LockFile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LockFile:
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.lock_path = self.workspace_path / LOCK_FILENAME
        self.data: dict[str, Any] = {}

    # ── Persistence ─────────────────────────────────────────

    def exists(self) -> bool:
        return self.lock_path.exists()

    def load(self) -> LockFile:
        if self.lock_path.exists():
            self.data = json.loads(self.lock_path.read_text())
        return self

    def save(self) -> None:
        self.data["updated_at"] = _now_iso()
        fd, tmp = tempfile.mkstemp(
            dir=self.workspace_path, suffix=".lock.tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.rename(tmp, self.lock_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def delete(self) -> None:
        if self.lock_path.exists():
            self.lock_path.unlink()

    # ── Lease logic ─────────────────────────────────────────

    def is_expired(self) -> bool:
        expires = self.data.get("lease_expires_at")
        if not expires:
            return True
        return datetime.fromisoformat(expires) < datetime.now(timezone.utc)

    def verify_active(self) -> None:
        """Raise if lock missing or expired."""
        if not self.exists():
            raise RuntimeError("no active lock for this workspace")
        self.load()
        if self.is_expired():
            self.delete()
            raise RuntimeError("lock lease expired (stale lock cleaned)")

    def renew_lease(self, minutes: int = DEFAULT_LEASE_MINUTES) -> None:
        self.data["lease_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ).isoformat()

    # ── Phase tracking ──────────────────────────────────────

    def update_phase(self, phase: str) -> None:
        old = self.data.get("phase")
        self.data["phase"] = phase
        history = self.data.setdefault("phase_history", [])
        if old:
            history.append({"phase": old, "completed_at": _now_iso()})

    def add_artifact(self, key: str, rel_path: str) -> None:
        self.data.setdefault("artifacts", {})[key] = rel_path

    # ── Factory ─────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        workspace_path: str,
        task: str,
        engine: str,
        env: str | None = None,
    ) -> LockFile:
        """Atomically create lock file using O_CREAT|O_EXCL.

        Raises FileExistsError if another session acquired the lock
        between our check and this call (race condition guard).
        """
        lf = cls(workspace_path)
        now = _now_iso()
        lf.data = {
            "task": task,
            "branch": None,
            "engine": engine,
            "env": env,
            "owner": {},
            "phase": "workspace-check",
            "phase_history": [],
            "artifacts": {},
            "pushed_to_remote": False,
            "started_at": now,
            "updated_at": now,
        }
        lf.renew_lease()
        # Atomic creation — fails if file already exists
        fd = os.open(
            str(lf.lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(lf.data, f, indent=2, ensure_ascii=False)
        except BaseException:
            # Clean up on failure
            if lf.lock_path.exists():
                lf.lock_path.unlink()
            raise
        return lf


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WorkspaceManager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class WorkspaceManager:
    def __init__(self, config: ConfigManager | None = None):
        self.config = config or ConfigManager()

    def check_and_acquire(
        self, name: str, task: str, engine: str
    ) -> dict:
        """Phase 0: check → acquire lock (atomic) → probe → return snapshot."""
        ws = self.config.get_workspace(name)
        if ws is None:
            return {"ok": False, "error": f"workspace '{name}' not found in config",
                    "error_code": "PATH_NOT_FOUND"}

        ws_path = ws["path"]
        if not Path(ws_path).exists():
            return {"ok": False, "error": f"path does not exist: {ws_path}",
                    "error_code": "PATH_NOT_FOUND"}

        # Check git repo
        if not (Path(ws_path) / ".git").exists():
            return {"ok": False, "error": f"not a git repository: {ws_path}",
                    "error_code": "PATH_NOT_FOUND"}

        # Check existing lock
        lock = LockFile(ws_path)
        if lock.exists():
            lock.load()
            if not lock.is_expired():
                return {
                    "ok": False,
                    "error": f"workspace busy: {lock.data.get('task', '?')} "
                             f"(phase: {lock.data.get('phase', '?')})",
                    "error_code": "WORKSPACE_LOCKED",
                }
            # stale lock — clean up
            lock.delete()

        # Check dirty working tree
        git_status = _run_git(ws_path, ["status", "--porcelain"])
        if git_status.strip():
            return {
                "ok": False,
                "error": "workspace has uncommitted changes, please commit or stash first",
                "error_code": "GIT_DIRTY",
            }

        # Probe git BEFORE any writes (so dirty flag reflects user's state)
        git_info = self._probe_git(ws_path)

        # Ensure .gitignore BEFORE lock creation (so lock file is ignored by git)
        gitignore_updated = self._ensure_gitignore(ws_path)

        # Acquire lock (atomic — O_CREAT|O_EXCL)
        try:
            lock = LockFile.create(ws_path, task=task, engine=engine)
        except FileExistsError:
            return {
                "ok": False,
                "error": "workspace was just acquired by another session",
                "error_code": "WORKSPACE_LOCKED",
            }

        # Build snapshot
        snapshot = {
            "workspace": ws,
            "git": git_info,
            "runtime": self._probe_runtime(ws_path),
            "project": self._probe_project(ws_path, ws),
            "gitignore_updated": gitignore_updated,
        }

        # Save artifacts
        art_dir = Path(ws_path) / ARTIFACT_DIR
        art_dir.mkdir(exist_ok=True)
        snap_path = art_dir / "workspace_snapshot.json"
        snap_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
        lock.add_artifact("workspace_snapshot", f"{ARTIFACT_DIR}/workspace_snapshot.json")
        lock.save()

        return {"ok": True, "data": {"snapshot": snapshot}}

    def release(self, name: str, cleanup: bool = False) -> dict:
        ws = self.config.get_workspace(name)
        if ws is None:
            return {"ok": False, "error": f"workspace '{name}' not found",
                    "error_code": "PATH_NOT_FOUND"}

        ws_path = ws["path"]
        lock = LockFile(ws_path)
        if not lock.exists():
            return {"ok": True, "data": {"message": "already released"}}

        lock.load()
        if cleanup:
            original_branch = "main"
            # try to get the branch from git snapshot
            snap_path = Path(ws_path) / ARTIFACT_DIR / "workspace_snapshot.json"
            if snap_path.exists():
                snap = json.loads(snap_path.read_text())
                original_branch = snap.get("git", {}).get("branch", "main")

            task_branch = lock.data.get("branch")
            if task_branch:
                _run_git(ws_path, ["checkout", original_branch])
                _run_git(ws_path, ["branch", "-D", task_branch])
                # Also delete remote branch if it was pushed
                if lock.data.get("pushed_to_remote"):
                    _run_git(ws_path, ["push", "--delete", "origin", task_branch])

        lock.delete()
        # Clean up artifact dir
        import shutil
        art_dir = Path(ws_path) / ARTIFACT_DIR
        if art_dir.exists():
            shutil.rmtree(art_dir, ignore_errors=True)

        return {"ok": True, "data": {"released": name, "cleanup": cleanup}}

    def renew_lease(self, name: str) -> dict:
        """Explicitly renew lease — call during long user-interaction waits."""
        ws = self.config.get_workspace(name)
        if ws is None:
            return {"ok": False, "error": f"workspace '{name}' not found",
                    "error_code": "PATH_NOT_FOUND"}

        ws_path = ws["path"]
        lock = LockFile(ws_path)
        if not lock.exists():
            return {"ok": False, "error": "no active lock for this workspace",
                    "error_code": "LOCK_NOT_FOUND"}
        lock.load()
        if lock.is_expired():
            lock.delete()
            return {"ok": False, "error": "lock lease already expired",
                    "error_code": "LEASE_EXPIRED"}
        lock.renew_lease()
        lock.save()
        return {"ok": True, "data": {
            "workspace": name,
            "lease_expires_at": lock.data["lease_expires_at"],
        }}

    # ── .gitignore management ────────────────────────────────

    @staticmethod
    def _ensure_gitignore(ws_path: str) -> bool:
        """Check .gitignore includes coding-master entries; append if missing.
        Returns True if entries were added."""
        gi_path = Path(ws_path) / ".gitignore"
        existing = ""
        if gi_path.exists():
            existing = gi_path.read_text()

        missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
        if not missing:
            return False

        with open(gi_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# coding-master artifacts\n")
            for entry in missing:
                f.write(entry + "\n")
        return True

    # ── Probing ─────────────────────────────────────────────

    def _probe_git(self, ws_path: str) -> dict:
        branch = _run_git(ws_path, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        dirty = bool(_run_git(ws_path, ["status", "--porcelain"]).strip())
        remote_url = _run_git(ws_path, ["remote", "get-url", "origin"]).strip()
        last_commit = _run_git(
            ws_path, ["log", "-1", "--format=%h %s"]
        ).strip()
        return {
            "branch": branch,
            "dirty": dirty,
            "remote_url": remote_url,
            "last_commit": last_commit,
        }

    def _probe_runtime(self, ws_path: str) -> dict:
        p = Path(ws_path)
        if (p / "pyproject.toml").exists() or (p / "setup.py").exists():
            version = _run_cmd(ws_path, ["python3", "--version"]).strip()
            # detect package manager
            pm = "pip"
            if (p / "uv.lock").exists():
                pm = "uv"
            elif (p / "poetry.lock").exists():
                pm = "poetry"
            elif (p / "Pipfile.lock").exists():
                pm = "pipenv"
            return {"type": "python", "version": version, "package_manager": pm}
        if (p / "package.json").exists():
            version = _run_cmd(ws_path, ["node", "--version"]).strip()
            pm = "npm"
            if (p / "pnpm-lock.yaml").exists():
                pm = "pnpm"
            elif (p / "yarn.lock").exists():
                pm = "yarn"
            elif (p / "bun.lockb").exists():
                pm = "bun"
            return {"type": "node", "version": version, "package_manager": pm}
        if (p / "Cargo.toml").exists():
            version = _run_cmd(ws_path, ["rustc", "--version"]).strip()
            return {"type": "rust", "version": version, "package_manager": "cargo"}
        if (p / "go.mod").exists():
            version = _run_cmd(ws_path, ["go", "version"]).strip()
            return {"type": "go", "version": version, "package_manager": "go"}
        return {"type": "unknown", "version": "", "package_manager": ""}

    def _probe_project(self, ws_path: str, ws_config: dict) -> dict:
        """Detect test/lint commands. Config overrides auto-detection."""
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
                # check for ruff config
                if _has_tool_in_pyproject(p / "pyproject.toml", "ruff"):
                    lint_cmd = "ruff check ."
                else:
                    lint_cmd = "python -m py_compile"
            elif (p / "package.json").exists():
                lint_cmd = "npm run lint"
            elif (p / "Cargo.toml").exists():
                lint_cmd = "cargo clippy"

        return {"test_command": test_cmd, "lint_command": lint_cmd}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_git(cwd: str, args: list[str]) -> str:
    return _run_cmd(cwd, ["git"] + args)


def _run_cmd(cwd: str, args: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _has_tool_in_pyproject(path: Path, tool: str) -> bool:
    try:
        text = path.read_text()
        return f"[tool.{tool}]" in text
    except Exception:
        return False
