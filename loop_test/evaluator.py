"""Claude Code subprocess wrapper for evaluating answers and driving fixes."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    passed: bool
    reason: str


def _run_claude(
    prompt: str,
    *,
    command: str = "claude",
    flags: Optional[list[str]] = None,
    extra_args: Optional[list[str]] = None,
    timeout: float = 300.0,
) -> str:
    """Run claude CLI with a prompt and return stdout."""
    if flags is None:
        flags = ["-p"]

    cmd = [command] + flags + [prompt]
    if extra_args:
        cmd.extend(extra_args)

    logger.info("Running claude: %s", " ".join(cmd)[:200])
    # Remove CLAUDECODE env var to allow nested claude invocations
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            logger.warning("claude exited %d: %s", result.returncode, result.stderr[:500])
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.error("claude timed out after %.0fs", timeout)
        return "[CLAUDE_TIMEOUT]"
    except FileNotFoundError:
        logger.error("claude command not found: %s", command)
        return "[CLAUDE_NOT_FOUND]"


def _parse_json_result(text: str) -> dict:
    """Try to extract JSON from claude's response."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON block in markdown
    for marker in ("```json", "```"):
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

    return {}


def check_answer(
    query: str,
    answer: str,
    expectation: str,
    *,
    command: str = "claude",
    flags: Optional[list[str]] = None,
    timeout: float = 180.0,
) -> CheckResult:
    """Use Claude Code to check if an agent's answer meets expectations.

    Returns CheckResult with pass/fail and reason.
    """
    prompt = (
        f"You are evaluating an AI agent's response quality.\n\n"
        f"User query: {query}\n\n"
        f"Agent answer:\n{answer}\n\n"
        f"Expected behavior: {expectation}\n\n"
        f"Does the answer meet the expectation? "
        f"Reply ONLY with JSON: {{\"pass\": true/false, \"reason\": \"brief explanation\"}}"
    )

    raw = _run_claude(
        prompt, command=command, flags=flags, timeout=timeout,
        extra_args=["--max-turns", "3"],
    )
    parsed = _parse_json_result(raw)

    if "pass" in parsed:
        return CheckResult(passed=bool(parsed["pass"]), reason=parsed.get("reason", ""))

    # Fallback: heuristic on raw text
    text_lower = raw.lower()
    if any(kw in text_lower for kw in ("pass", "meets", "satisf", "符合", "通过")):
        return CheckResult(passed=True, reason=raw[:300])
    return CheckResult(passed=False, reason=raw[:300])


def analyze_logs(
    alfred_home: str,
    agent_name: str,
    *,
    command: str = "claude",
    flags: Optional[list[str]] = None,
    timeout: float = 180.0,
) -> str:
    """Use Claude Code to analyze agent logs and traces for issues."""
    home = Path(alfred_home).expanduser()
    prompt = (
        f"请看 {home} 下刚发送给 {agent_name} 的会话日志和轨迹，看有什么问题。"
        f"重点关注：\n"
        f"1. {home}/sessions/ 下最新的 session 文件\n"
        f"2. {home}/agents/{agent_name}/tmp/ 下的 trajectory 文件\n"
        f"请分析问题并给出具体的原因。"
    )
    return _run_claude(prompt, command=command, flags=flags, timeout=timeout)


def suggest_testcases(
    analysis: str,
    *,
    command: str = "claude",
    flags: Optional[list[str]] = None,
    timeout: float = 180.0,
) -> str:
    """Use Claude Code to suggest and add test cases based on the analysis."""
    prompt = (
        f"如果是这个问题：\n{analysis}\n\n"
        f"为什么 testcases 没覆盖？请补充优化 testcase，确保 fail。"
        f"请直接修改测试文件，添加能暴露这个问题的测试用例。"
    )
    return _run_claude(prompt, command=command, flags=flags, timeout=timeout)


def fix_and_regress(
    suggestion: str,
    *,
    command: str = "claude",
    flags: Optional[list[str]] = None,
    timeout: float = 180.0,
) -> str:
    """Use Claude Code to fix code and run regression tests."""
    prompt = (
        f"好，现在开始修复，后回归，确保刚补充的 testcases 通过。\n\n"
        f"上一步的分析和建议：\n{suggestion}\n\n"
        f"请修复代码并运行相关测试确认通过。"
    )
    return _run_claude(prompt, command=command, flags=flags, timeout=timeout)
