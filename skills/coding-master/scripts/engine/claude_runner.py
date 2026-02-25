"""Claude Code headless engine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engine import CodingEngine, EngineResult


class ClaudeRunner(CodingEngine):
    def run(
        self,
        repo_path: str,
        prompt: str,
        max_turns: int = 30,
        timeout: int = 600,
    ) -> EngineResult:
        cmd = [
            "claude",
            "-p", prompt,
            "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
            "--output-format", "json",
            "--max-turns", str(max_turns),
        ]

        try:
            r = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                success=False,
                summary="",
                error=f"claude timed out after {timeout}s",
            )
        except FileNotFoundError:
            return EngineResult(
                success=False,
                summary="",
                error="claude CLI not found — is Claude Code installed?",
            )

        # Parse output
        summary = ""
        try:
            data = json.loads(r.stdout)
            # Claude JSON output has a "result" field with the response text
            summary = data.get("result", r.stdout[:5000])
        except (json.JSONDecodeError, TypeError):
            summary = r.stdout[:5000] if r.stdout else r.stderr[:2000]

        if r.returncode != 0 and not summary:
            return EngineResult(
                success=False,
                summary=summary,
                error=f"claude exited with code {r.returncode}: {r.stderr[:1000]}",
            )

        # Detect changed files via git diff
        files_changed = _get_changed_files(repo_path)

        return EngineResult(
            success=True,
            summary=summary,
            files_changed=files_changed,
        )


def _get_changed_files(repo_path: str) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = set()
        for line in (r.stdout + "\n" + staged.stdout).splitlines():
            line = line.strip()
            if line:
                files.add(line)
        return sorted(files)
    except Exception:
        return []
