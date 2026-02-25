#!/usr/bin/env python3
"""Git operations: branch, commit, push, PR."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROTECTED_BRANCHES = {"main", "master"}


class GitOps:
    def __init__(self, workspace_path: str):
        self.path = workspace_path

    def get_current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def create_branch(self, branch: str) -> dict:
        current = self.get_current_branch()
        if branch == current:
            return {"ok": True, "data": {"branch": branch, "created": False}}
        out = self._git("checkout", "-b", branch)
        return {"ok": True, "data": {"branch": branch, "created": True, "from": current}}

    def get_diff_summary(self) -> str:
        return self._git("diff", "--stat")

    def stage_and_commit(self, message: str) -> dict:
        # Stage all changes
        self._git("add", "-A")
        # Check if there's anything to commit
        status = self._git("status", "--porcelain")
        if not status.strip():
            return {"ok": False, "error": "nothing to commit"}
        self._git("commit", "-m", message)
        return {"ok": True, "data": {"message": message}}

    def push(self, branch: str | None = None) -> dict:
        branch = branch or self.get_current_branch()
        if branch in PROTECTED_BRANCHES:
            return {"ok": False, "error": f"refusing to push to protected branch: {branch}"}
        out, err, rc = self._git_full("push", "-u", "origin", branch)
        if rc != 0:
            return {"ok": False, "error": f"push failed: {err}"}
        return {"ok": True, "data": {"branch": branch}}

    def create_pr(self, title: str, body: str) -> dict:
        try:
            r = subprocess.run(
                ["gh", "pr", "create", "--title", title, "--body", body],
                cwd=self.path,
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            return {"ok": False, "error": "gh CLI not found — install GitHub CLI"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "gh pr create timed out"}

        if r.returncode != 0:
            return {"ok": False, "error": f"gh pr create failed: {r.stderr}"}

        pr_url = r.stdout.strip()
        return {"ok": True, "data": {"pr_url": pr_url}}

    def submit_pr(self, title: str, body: str, commit_message: str | None = None) -> dict:
        """Full sequence: add → commit → push → pr create."""
        branch = self.get_current_branch()
        if branch in PROTECTED_BRANCHES:
            return {"ok": False, "error": f"cannot submit PR from protected branch: {branch}"}

        # commit
        msg = commit_message or title
        commit_result = self.stage_and_commit(msg)
        if not commit_result.get("ok"):
            return commit_result

        # push
        push_result = self.push(branch)
        if not push_result.get("ok"):
            return push_result

        # create PR
        pr_result = self.create_pr(title, body)
        return pr_result

    def cleanup_branch(self, original_branch: str, task_branch: str) -> dict:
        """Checkout original branch and delete task branch."""
        current = self.get_current_branch()
        if current == task_branch:
            self._git("checkout", original_branch)
        _, err, rc = self._git_full("branch", "-D", task_branch)
        if rc != 0:
            return {"ok": False, "error": f"failed to delete branch: {err}"}
        return {"ok": True, "data": {"deleted": task_branch, "on": original_branch}}

    # ── Internals ───────────────────────────────────────────

    def _git(self, *args: str) -> str:
        r = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout

    def _git_full(self, *args: str) -> tuple[str, str, int]:
        r = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout, r.stderr, r.returncode
