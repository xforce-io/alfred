"""SkillLogRecorder — adapts TurnEvent/dict events to SLM SkillLog writes.

Responsibilities:
1. Filter: skip internal tools (names starting with "_")
2. Version read: from skills/{skill_id}/SKILL.md frontmatter, fallback "baseline"
3. Write: construct EvaluationSegment and append via SegmentLogger

Concurrency note: SegmentLogger.append() is synchronous open/write/close.
In the current asyncio single-threaded model this is safe — asyncio
coroutines do not yield during synchronous I/O. If the architecture moves
to multi-process, add fcntl.flock around the write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import EvaluationSegment
from .segment_logger import SegmentLogger
from .version_manager import read_frontmatter_version

logger = logging.getLogger(__name__)


class SkillLogRecorder:
    """Adapts skill invocation events to SLM log writes.

    Invariant: all internal tools start with "_". Any skill_name NOT starting
    with "_" is treated as a user-level skill and will be recorded.
    If this invariant ever changes, update the filter rule in maybe_record().
    """

    def __init__(self, skill_logs_dir: Path, skills_dir: Path) -> None:
        # SegmentLogger is stateless across calls (open/write/close each time)
        # so sharing one instance is safe.
        self._logger = SegmentLogger(skill_logs_dir)
        self._skills_dir = skills_dir

    def maybe_record(
        self,
        skill_name: Optional[str],
        *,
        session_id: str,
        skill_output: Optional[str] = "",
        context_before: Optional[str] = "",
    ) -> bool:
        """Record a skill invocation to the SLM log.

        The ``skill_name`` parameter accepts ``None`` for defensive callers;
        None or empty string is treated as "no skill" and returns False.

        ``context_before`` and ``skill_output`` accept ``None`` defensively;
        both are normalised to empty string before writing.

        Returns:
            True  — log written successfully.
            False — skipped (internal tool, boundary check) or write failed.

        Failures are logged at WARNING level and never propagate — this is a
        side-channel recorder and must never block the main session flow.
        """
        # Boundary check (accepts Optional[str] defensively)
        if not skill_name:
            return False
        # Filter internal tools (all starting with "_")
        if skill_name.startswith("_"):
            return False

        try:
            skill_md_path = self._skills_dir / skill_name / "SKILL.md"
            version = read_frontmatter_version(skill_md_path)
            segment = EvaluationSegment(
                skill_id=skill_name,
                skill_version=version,
                triggered_at=datetime.now(timezone.utc).isoformat(),
                context_before=context_before or "",
                skill_output=skill_output or "",
                context_after="",  # v1: not available at write time; requires cross-turn state
                session_id=session_id,
            )
            self._logger.append(segment)
            return True
        except Exception as e:
            # Catch all exceptions (OSError, UnicodeDecodeError, etc.) so that
            # log-write failures never crash the main session flow.
            logger.warning(
                "SkillLogRecorder: failed to write log for skill '%s': %s", skill_name, e
            )
            return False


def handle_skill_event(
    event: Any,
    recorder: SkillLogRecorder,
    *,
    session_id: str,
    context_before: str = "",
) -> bool:
    """Handle a TurnEvent object from CoreService path for SLM logging.

    Only processes SKILL events with status="completed". All other event
    types and statuses return False without side effects.

    When a raw dict is passed (TurnExecutor/heartbeat path), this function
    returns False — use record_skills_from_raw_events() for that path instead.

    Args:
        event: A TurnEvent instance (or any object with .type / .status /
               .skill_name / .skill_output attributes).
        recorder: The SkillLogRecorder to write to.
        session_id: Current session identifier.
        context_before: Text preceding the skill invocation (user message).

    Returns:
        True if a log entry was written, False otherwise.
    """
    # Import here to keep slm/ dependency on runtime/ lazy (avoids import-time issues)
    from ..runtime.turn_policy import TurnEventType

    if getattr(event, "type", None) != TurnEventType.SKILL:
        return False
    if (getattr(event, "status", "") or "").lower() != "completed":
        return False
    return recorder.maybe_record(
        getattr(event, "skill_name", None),
        session_id=session_id,
        skill_output=getattr(event, "skill_output", None),
        context_before=context_before,
    )


def record_skills_from_raw_events(
    raw_events: List[Dict[str, Any]],
    recorder: SkillLogRecorder,
    *,
    session_id: str,
    context_before: str = "",
) -> int:
    """Extract SKILL completed events from TurnExecutor's raw dict list and record them.

    This is the heartbeat path entry point. TurnResult.events is a
    List[Dict[str, Any]] produced by TurnExecutor._turn_event_to_raw().

    Expected raw dict format for SKILL events::

        {
            "_progress": [{
                "stage": "skill",
                "skill_info": {"name": <str>, "args": <str>},
                "answer": <str>,
                "id": <str>,
                "status": <str>
            }]
        }

    Note: context_before is shared across all skill events in one turn (the
    trigger message). In a multi-skill turn this is a v1 simplification —
    each skill gets the same trigger message as context_before regardless of
    execution order.

    NOTE: This function is coupled to the output format of
    TurnExecutor._turn_event_to_raw(). If the SKILL branch of that method
    changes, this function must be updated to match.

    Returns:
        Number of skill log entries successfully written.
    """
    count = 0
    for evt in raw_events:
        if not isinstance(evt, dict):
            continue
        progress_list = evt.get("_progress", [])
        # Defensively handle non-list _progress values (e.g. None, int, str).
        if not isinstance(progress_list, (list, tuple)):
            continue
        for progress in progress_list:
            if not isinstance(progress, dict):
                continue
            if progress.get("stage") != "skill":
                continue
            if (progress.get("status") or "").lower() != "completed":
                continue
            skill_info = progress.get("skill_info") or {}
            if not isinstance(skill_info, dict):
                continue
            name = skill_info.get("name", "")
            if recorder.maybe_record(
                name,
                session_id=session_id,
                skill_output=progress.get("answer", ""),
                context_before=context_before,
            ):
                count += 1
    return count
