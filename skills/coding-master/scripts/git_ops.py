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

    # ── Static repo operations ─────────────────────────────

    @staticmethod
    def clone(url: str, target_path: str, branch: str | None = None) -> dict:
        """Clone a repo to target_path. Optionally checkout a specific branch."""
        cmd = ["git", "clone", url, target_path]
        if branch:
            cmd.extend(["--branch", branch])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git clone timed out"}
        if r.returncode != 0:
            return {"ok": False, "error": f"git clone failed: {r.stderr.strip()}"}
        return {"ok": True, "data": {"path": target_path}}

    @staticmethod
    def fetch(repo_path: str) -> dict:
        """Run git fetch in repo_path."""
        try:
            r = subprocess.run(
                ["git", "fetch"],
                cwd=repo_path,
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git fetch timed out"}
        if r.returncode != 0:
            return {"ok": False, "error": f"git fetch failed: {r.stderr.strip()}"}
        return {"ok": True, "data": {"path": repo_path}}

    @staticmethod
    def pull(repo_path: str, branch: str | None = None) -> dict:
        """Optionally checkout branch, then git pull."""
        if branch:
            r = subprocess.run(
                ["git", "checkout", branch],
                cwd=repo_path,
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return {"ok": False, "error": f"git checkout failed: {r.stderr.strip()}"}
        try:
            r = subprocess.run(
                ["git", "pull"],
                cwd=repo_path,
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git pull timed out"}
        if r.returncode != 0:
            return {"ok": False, "error": f"git pull failed: {r.stderr.strip()}"}
        return {"ok": True, "data": {"path": repo_path, "branch": branch}}

    # ── Stash operations ──────────────────────────────────────

    def stash_save(self, message: str = "") -> dict:
        """Save working directory changes to stash."""
        args = ["stash", "push"]
        if message:
            args.extend(["-m", message])
        out, err, rc = self._git_full(*args)
        if rc != 0:
            return {"ok": False, "error": f"git stash push failed: {err}"}
        if "No local changes" in out:
            return {"ok": True, "data": {"stashed": False}}
        return {"ok": True, "data": {"stashed": True, "message": message}}

    def stash_pop(self) -> dict:
        """Restore most recent stash entry."""
        out, err, rc = self._git_full("stash", "pop")
        if rc != 0:
            return {"ok": False, "error": f"git stash pop failed: {err}"}
        return {"ok": True, "data": {"restored": True}}

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
