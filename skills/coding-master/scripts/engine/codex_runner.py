"""OpenAI Codex CLI headless engine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engine import CodingEngine, EngineResult


class CodexRunner(CodingEngine):
    def run(
        self,
        repo_path: str,
        prompt: str,
        max_turns: int = 30,
        timeout: int = 600,
    ) -> EngineResult:
        cmd = [
            "codex", "exec",
            "--full-auto",
            "--json",
            "-C", repo_path,
            prompt,
        ]

        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                success=False,
                summary="",
                error=f"codex timed out after {timeout}s",
            )
        except FileNotFoundError:
            return EngineResult(
                success=False,
                summary="",
                error="codex CLI not found — is Codex installed?",
            )

        # Parse JSONL output — extract last message as summary
        summary = ""
        has_valid_output = False
        try:
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            if lines:
                last = json.loads(lines[-1])
                has_valid_output = True
                # Codex JSONL: look for message content in the last entry
                if isinstance(last, dict):
                    summary = (
                        last.get("message", "")
                        or last.get("content", "")
                        or last.get("result", "")
                        or json.dumps(last)[:5000]
                    )
                else:
                    summary = str(last)[:5000]
            else:
                summary = r.stdout[:5000] if r.stdout else r.stderr[:2000]
        except (json.JSONDecodeError, TypeError):
            summary = r.stdout[:5000] if r.stdout else r.stderr[:2000]

        if r.returncode != 0 and not has_valid_output:
            return EngineResult(
                success=False,
                summary=summary,
                error=f"codex exited with code {r.returncode}: {r.stderr[:1000]}",
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
