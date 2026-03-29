"""Unified guard logic for heartbeat task execution.

TaskExecutionGate centralises scanner-gate and min_execution_interval
checks that were previously duplicated across inline / isolated /
execute_isolated_claimed_task code-paths.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class GateVerdict:
    """Result of a TaskExecutionGate.check() call."""

    allowed: bool
    skip_reason: Optional[str] = None  # "no_changes" | "interval_not_met" | "scanner_error"
    scan_result: Any = None


class TaskExecutionGate:
    """Unified pre-execution guard for skill tasks.

    Checks (in order):
      1. Scanner gate  – has the monitored data source changed since watermark?
      2. Min-interval  – has enough wall-clock time passed since last_run_at?

    After successful execution the caller should invoke ``commit()`` to
    advance the watermark.
    """

    def __init__(
        self,
        workspace_path: Path,
        agent_name: str,
        scanner_factory: Callable[[Optional[str]], Any],
    ):
        self._workspace_path = Path(workspace_path)
        self._agent_name = agent_name
        self._scanner_factory = scanner_factory

    # ── public API ────────────────────────────────────────────

    def check(self, task: Any) -> GateVerdict:
        """Run all guard checks for *task*.  Returns a GateVerdict."""
        scanner_type = getattr(task, "scanner", None)
        scanner = self._scanner_factory(scanner_type)
        scan_result = None

        # 1. Scanner gate
        if scanner:
            from ..scanners.reflection_state import ReflectionState

            skill_name = getattr(task, "job", None) or ""
            state = ReflectionState.load(self._workspace_path)
            try:
                scan_result = scanner.check(state.get_watermark(skill_name), self._agent_name)
            except Exception as exc:
                logger.warning("Scanner %s error: %s", scanner_type, exc)
                return GateVerdict(allowed=False, skip_reason="scanner_error", scan_result=None)
            if not scan_result.has_changes:
                return GateVerdict(allowed=False, skip_reason="no_changes", scan_result=scan_result)

        # 2. Min execution interval
        if not self._check_min_execution_interval(task):
            return GateVerdict(allowed=False, skip_reason="interval_not_met", scan_result=scan_result)

        return GateVerdict(allowed=True, scan_result=scan_result)

    def commit(self, task: Any, verdict: GateVerdict) -> None:
        """Advance watermark after successful execution.

        Uses ``datetime.now(UTC)`` – **not** scan-time ``updated_at`` – to
        prevent self-triggering loops when skill execution itself mutates the
        monitored data source.
        """
        from ..scanners.reflection_state import ReflectionState

        job_name = getattr(task, "job", None) or ""
        if not job_name:
            return
        state = ReflectionState.load(self._workspace_path)
        state.set_watermark(job_name, datetime.now(timezone.utc).isoformat())
        state.save(self._workspace_path)

    # ── internal helpers ──────────────────────────────────────

    @staticmethod
    def _check_min_execution_interval(task: Any) -> bool:
        """Return True if enough time has elapsed since *task.last_run_at*."""
        from .task_manager import parse_iso_datetime

        min_interval = getattr(task, "min_execution_interval", None)
        if not min_interval:
            return True
        last_run = getattr(task, "last_run_at", None)
        if not last_run:
            return True
        last_dt = parse_iso_datetime(last_run)
        if last_dt is None:
            return True
        interval_match = re.fullmatch(r"(\d+)([mhd])", str(min_interval).strip())
        if not interval_match:
            return True
        amount = int(interval_match.group(1))
        unit = interval_match.group(2)
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        now = datetime.now(timezone.utc)
        return now >= last_dt + delta
