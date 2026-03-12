"""Engine abstraction for delegating code analysis to subprocess-based LLM tools.

Provides CodingEngine ABC and ClaudeCodeEngine implementation that invokes
the Claude Code CLI (`claude -p ...`) as a subprocess.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Output from engine is truncated to this many bytes to prevent context explosion
OUTPUT_MAX_BYTES = 50_000

# Mode-specific prompt templates
MODE_PROMPTS = {
    "review": (
        "You are a code reviewer. Analyze the code changes described below.\n"
        "Focus on: correctness, security, performance, maintainability.\n"
        "Return structured findings with file paths, line numbers, severity "
        "(critical/warning/info), and descriptions.\n\n"
        "IMPORTANT: Output your findings as a JSON object with this structure:\n"
        '{{"summary": "...", "findings": [{{"file": "...", "lines": "...", '
        '"severity": "...", "description": "..."}}], '
        '"files_analyzed": ["..."]}}\n\n'
    ),
    "analyze": (
        "You are a code analyst. Understand the code described below and "
        "produce structured conclusions.\n"
        "Focus on: architecture, patterns, dependencies, potential issues.\n\n"
        "IMPORTANT: Output your analysis as a JSON object with this structure:\n"
        '{{"summary": "...", "findings": [{{"file": "...", "lines": "...", '
        '"severity": "...", "description": "..."}}], '
        '"files_analyzed": ["..."]}}\n\n'
    ),
    "debug": (
        "You are a debugger. Investigate the issue described below.\n"
        "Focus on: root cause, reproduction steps, affected code paths.\n\n"
        "IMPORTANT: Output your diagnosis as a JSON object with this structure:\n"
        '{{"summary": "...", "findings": [{{"file": "...", "lines": "...", '
        '"severity": "...", "description": "..."}}], '
        '"files_analyzed": ["..."]}}\n\n'
    ),
    "deliver": (
        "You are a software engineer. Implement the changes described below.\n"
        "Read the relevant code, make the necessary edits, and run tests.\n"
        "Focus on: correctness, test coverage, minimal diff.\n\n"
        "IMPORTANT: Output your result as a JSON object with this structure:\n"
        '{{"summary": "...", "files_changed": ["..."], '
        '"files_analyzed": ["..."]}}\n\n'
    ),
}

# Allowed tools per mode
MODE_TOOLS = {
    "review": "Read,Glob,Grep",
    "analyze": "Read,Glob,Grep",
    "debug": "Read,Glob,Grep,Bash",
    "deliver": "Read,Edit,Write,Glob,Grep,Bash",
}


@dataclass
class EngineResult:
    """Structured result from an engine run."""

    ok: bool = True
    summary: str = ""
    files_analyzed: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    error: str = ""
    engine: str = ""
    turns_used: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "files_analyzed": self.files_analyzed,
            "files_changed": self.files_changed,
            "findings": self.findings,
            "error": self.error,
            "engine": self.engine,
            "turns_used": self.turns_used,
        }


class CodingEngine(ABC):
    """Abstract base for code analysis engines."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def run(
        self,
        prompt: str,
        repo_path: str | Path,
        *,
        mode: str = "review",
        timeout: int = 600,
        max_turns: int = 30,
    ) -> EngineResult: ...


class ClaudeCodeEngine(CodingEngine):
    """Engine that delegates to the Claude Code CLI."""

    def name(self) -> str:
        return "claude-code"

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def run(
        self,
        prompt: str,
        repo_path: str | Path,
        *,
        mode: str = "review",
        timeout: int = 600,
        max_turns: int = 30,
    ) -> EngineResult:
        if not self.is_available():
            return EngineResult(
                ok=False,
                error="claude CLI not found in PATH. Install Claude Code first.",
                engine=self.name(),
            )

        repo_path = Path(repo_path).resolve()
        if not repo_path.is_dir():
            return EngineResult(
                ok=False,
                error=f"repo_path is not a valid directory: {repo_path}",
                engine=self.name(),
            )
        allowed_tools = MODE_TOOLS.get(mode, MODE_TOOLS["review"])

        cmd = [
            "claude",
            "-p", prompt,
            "--allowedTools", allowed_tools,
            "--output-format", "json",
            "--max-turns", str(max_turns),
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                error=f"Engine timed out after {timeout}s",
                engine=self.name(),
            )
        except Exception as exc:
            return EngineResult(
                ok=False,
                error=f"Engine subprocess failed: {exc}",
                engine=self.name(),
            )

        # Parse JSON output from claude CLI
        raw_output = result.stdout
        if len(raw_output) > OUTPUT_MAX_BYTES:
            raw_output = raw_output[:OUTPUT_MAX_BYTES] + "\n...(truncated)..."

        if result.returncode != 0:
            return EngineResult(
                ok=False,
                error=f"claude exited with code {result.returncode}: "
                      f"{(result.stderr or raw_output)[:2000]}",
                engine=self.name(),
            )

        return self._parse_output(raw_output)

    def _parse_output(self, raw_output: str) -> EngineResult:
        """Parse claude CLI JSON output into EngineResult."""
        # Claude CLI --output-format json returns a JSON object with "result" key
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError:
            # Non-JSON output — treat as plain text summary
            return EngineResult(
                ok=True,
                summary=raw_output[:5000],
                engine=self.name(),
            )

        # Extract from claude CLI JSON envelope
        result_text = ""
        turns_used = 0

        if isinstance(data, dict):
            result_text = data.get("result", "")
            turns_used = data.get("num_turns", 0)
        elif isinstance(data, str):
            result_text = data

        # Try to parse the result text as structured JSON
        findings = []
        files_analyzed = []
        summary = result_text

        try:
            parsed = json.loads(result_text)
            if isinstance(parsed, dict):
                summary = parsed.get("summary", result_text[:2000])
                findings = parsed.get("findings", [])
                files_analyzed = parsed.get("files_analyzed", [])
        except (json.JSONDecodeError, TypeError):
            # result_text is plain text, use as summary
            if len(summary) > 5000:
                summary = summary[:5000] + "..."

        return EngineResult(
            ok=True,
            summary=summary,
            files_analyzed=files_analyzed,
            findings=findings,
            engine=self.name(),
            turns_used=turns_used,
        )


# Engine registry
_ENGINES: dict[str, type[CodingEngine]] = {
    "claude-code": ClaudeCodeEngine,
}


def get_engine(name: str = "claude-code") -> CodingEngine:
    """Get an engine instance by name.

    Raises ValueError if the engine name is unknown.
    """
    cls = _ENGINES.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown engine: {name!r}. Available: {', '.join(_ENGINES)}"
        )
    return cls()
