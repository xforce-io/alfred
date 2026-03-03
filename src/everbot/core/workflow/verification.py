"""Verification command execution and result extraction."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from .models import CmdResult, VerificationCmdConfig

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 4000


async def run_verification_cmd(
    config: VerificationCmdConfig,
    *,
    skill_dir: str,
    project_dir: str,
    session_id: str,
) -> CmdResult:
    """Execute a verification command as a subprocess.

    Environment variables ``$SKILL_DIR``, ``$PROJECT_DIR``, and
    ``$WORKFLOW_SESSION_ID`` are injected into the subprocess environment.
    stdout + stderr are captured and truncated to 4000 chars.
    """
    env = dict(os.environ)
    env["SKILL_DIR"] = skill_dir
    env["PROJECT_DIR"] = project_dir
    env["WORKFLOW_SESSION_ID"] = session_id
    env.update(config.env)

    working_dir = config.working_dir or project_dir

    logger.info(
        "workflow.verification.start",
        extra={
            "cmd": config.cmd,
            "timeout": config.timeout_seconds,
            "working_dir": working_dir,
        },
    )

    try:
        proc = await asyncio.create_subprocess_shell(
            config.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=config.timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            msg = (
                f"Verification command timed out after {config.timeout_seconds}s: "
                f"{config.cmd}"
            )
            logger.warning("workflow.verification.timeout", extra={"cmd": config.cmd})
            return CmdResult(exit_code=1, output=msg)

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n... [truncated, total {len(output)} chars]"

        logger.info(
            "workflow.verification.done",
            extra={
                "cmd": config.cmd,
                "exit_code": proc.returncode,
                "output_len": len(output),
            },
        )
        return CmdResult(exit_code=proc.returncode or 0, output=output)

    except Exception as exc:
        msg = f"Verification command failed to execute: {exc}"
        logger.error("workflow.verification.error", extra={"cmd": config.cmd, "error": str(exc)})
        return CmdResult(exit_code=1, output=msg)


def extract_verify_result(artifact: str, protocol: Optional[str]) -> bool:
    """Extract pass/fail from LLM verify output.

    Supports ``structured_tag`` protocol: looks for
    ``<verify_result>PASS</verify_result>`` or
    ``<verify_result>FAIL: reason</verify_result>``.

    Returns False (conservative) if tag is not found.
    """
    if protocol != "structured_tag":
        return False

    match = re.search(
        r"<verify_result>(.*?)</verify_result>", artifact, re.DOTALL
    )
    if not match:
        logger.warning("workflow.verify.tag_missing")
        return False

    content = match.group(1).strip().upper()
    if content == "PASS":
        return True

    # FAIL or FAIL: reason
    return False
