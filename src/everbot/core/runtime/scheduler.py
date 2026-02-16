"""Unified scheduler: heartbeat intervals + cron task dispatch.

The Scheduler owns **all** scheduling decisions:

- Heartbeat ticks (interval-based, per agent)
- Inline tasks (merged into the heartbeat turn that triggered them)
- Isolated tasks (claimed then dispatched to a dedicated job session)

HeartbeatRunner is a pure executor — it never decides *when* to run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SchedulerTask:
    """Minimal scheduler-facing task shape."""

    id: str
    agent_name: str
    execution_mode: str = "inline"
    timeout_seconds: int = 120


@dataclass
class AgentSchedule:
    """Per-agent heartbeat scheduling state."""

    agent_name: str
    interval_minutes: int = 30
    next_heartbeat_at: Optional[datetime] = None
    active_hours: tuple[int, int] = (8, 22)
    consecutive_failures: int = 0
    max_backoff_minutes: int = 60


class Scheduler:
    """Unified scheduler: heartbeat intervals + cron task dispatch.

    One ``tick()`` call handles everything:

    1. Trigger due heartbeat ticks (interval-driven, per agent).
    2. Collect due tasks, split by ``execution_mode``.
    3. Merge inline tasks into the heartbeat turn.
    4. Claim and dispatch isolated tasks independently.

    All callbacks are injected — the scheduler has zero knowledge of
    agents, sessions, or LLM execution.
    """

    def __init__(
        self,
        *,
        # --- Task callbacks (optional, for cron task support) ---
        get_due_tasks: Optional[Callable[[datetime], Iterable[SchedulerTask]]] = None,
        claim_task: Optional[Callable[[str], Awaitable[bool]]] = None,
        run_inline: Optional[Callable[[str, List[SchedulerTask], datetime], Awaitable[Any]]] = None,
        run_isolated: Optional[Callable[[SchedulerTask, datetime], Awaitable[Any]]] = None,
        # --- Heartbeat callback ---
        run_heartbeat: Optional[Callable[[str, datetime], Awaitable[Any]]] = None,
        # --- Config ---
        agent_schedules: Optional[Dict[str, AgentSchedule]] = None,
        tick_interval_seconds: float = 1.0,
        state_file: Optional[Path] = None,
    ):
        self._get_due_tasks = get_due_tasks
        self._claim_task = claim_task
        self._run_inline = run_inline
        self._run_isolated = run_isolated
        self._run_heartbeat = run_heartbeat
        self._agent_schedules: Dict[str, AgentSchedule] = dict(agent_schedules or {})
        self._tick_interval_seconds = tick_interval_seconds
        self._running = False
        self._state_file = state_file
        self._restore_state()

    # -- State persistence --------------------------------------------------

    def _save_state(self) -> None:
        """Persist next_heartbeat_at timestamps and consecutive_failures to disk."""
        if self._state_file is None:
            return
        try:
            state = {}
            for name, sched in self._agent_schedules.items():
                entry: Dict[str, Any] = {}
                if sched.next_heartbeat_at is not None:
                    entry["next_heartbeat_at"] = sched.next_heartbeat_at.isoformat()
                entry["consecutive_failures"] = sched.consecutive_failures
                state[name] = entry
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Failed to save scheduler state", exc_info=True)

    def _restore_state(self) -> None:
        """Restore next_heartbeat_at and consecutive_failures from disk.

        Handles both legacy format (value is ISO string) and new format
        (value is dict with ``next_heartbeat_at`` and ``consecutive_failures``).
        """
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
            for name, value in state.items():
                if name not in self._agent_schedules:
                    continue
                sched = self._agent_schedules[name]
                if isinstance(value, str):
                    # Legacy format: plain ISO timestamp string
                    try:
                        sched.next_heartbeat_at = datetime.fromisoformat(value)
                    except (ValueError, TypeError):
                        pass
                elif isinstance(value, dict):
                    iso_str = value.get("next_heartbeat_at")
                    if iso_str:
                        try:
                            sched.next_heartbeat_at = datetime.fromisoformat(iso_str)
                        except (ValueError, TypeError):
                            pass
                    sched.consecutive_failures = int(value.get("consecutive_failures", 0) or 0)
            logger.debug("Restored scheduler state from %s", self._state_file)
        except Exception:
            logger.debug("Failed to restore scheduler state", exc_info=True)

    # -- Agent schedule management ------------------------------------------

    def register_agent(self, schedule: AgentSchedule) -> None:
        """Register or update an agent's heartbeat schedule."""
        self._agent_schedules[schedule.agent_name] = schedule

    def unregister_agent(self, agent_name: str) -> None:
        self._agent_schedules.pop(agent_name, None)

    # -- Core tick ----------------------------------------------------------

    async def tick(self, now: Optional[datetime] = None) -> None:
        """Execute one scheduling tick (heartbeat intervals + cron tasks)."""
        ts = now or datetime.now()

        # Phase 1: Trigger due heartbeat ticks
        await self._tick_heartbeats(ts)

        # Phase 2: Trigger due cron tasks (inline + isolated)
        await self._tick_tasks(ts)

    async def _tick_heartbeats(self, ts: datetime) -> None:
        if self._run_heartbeat is None:
            return
        for schedule in list(self._agent_schedules.values()):
            if not self._is_active_time(schedule, ts):
                continue
            if schedule.next_heartbeat_at is not None and ts < schedule.next_heartbeat_at:
                continue
            base_interval = max(1, schedule.interval_minutes)
            try:
                await self._run_heartbeat(schedule.agent_name, ts)
                # Success: reset backoff, schedule at normal interval
                schedule.consecutive_failures = 0
                schedule.next_heartbeat_at = ts + timedelta(minutes=base_interval)
            except Exception:
                schedule.consecutive_failures += 1
                backoff_minutes = min(
                    base_interval * (2 ** schedule.consecutive_failures),
                    schedule.max_backoff_minutes,
                )
                schedule.next_heartbeat_at = ts + timedelta(minutes=backoff_minutes)
                logger.exception(
                    "Heartbeat tick failed for %s (consecutive_failures=%d, next_retry_in=%d min)",
                    schedule.agent_name,
                    schedule.consecutive_failures,
                    backoff_minutes,
                )
            self._save_state()

    async def _tick_tasks(self, ts: datetime) -> None:
        if self._get_due_tasks is None:
            return
        try:
            due_tasks = list(self._get_due_tasks(ts) or [])
        except Exception:
            logger.exception("Failed to collect due tasks")
            return

        inline_tasks, isolated_tasks = self._split_tasks(due_tasks)

        # Inline: merge by agent
        if self._run_inline is not None and inline_tasks:
            inline_by_agent: Dict[str, List[SchedulerTask]] = {}
            for task in inline_tasks:
                inline_by_agent.setdefault(task.agent_name, []).append(task)
            for agent_name, tasks in inline_by_agent.items():
                try:
                    await self._run_inline(agent_name, tasks, ts)
                except Exception:
                    logger.exception("Inline task execution failed for %s", agent_name)

        # Isolated: claim then dispatch, per-task error isolation
        if self._claim_task is not None and self._run_isolated is not None:
            for task in isolated_tasks:
                try:
                    claimed = await self._claim_task(task.id)
                    if not claimed:
                        continue
                    await self._run_isolated(task, ts)
                except Exception:
                    logger.exception("Isolated task %s failed", task.id)

    # -- Main loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        """Run scheduler loop until stopped."""
        self._running = True
        try:
            while self._running:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Scheduler tick failed")
                await asyncio.sleep(self._tick_interval_seconds)
        finally:
            self._running = False

    def stop(self) -> None:
        """Stop scheduler loop."""
        self._running = False

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _is_active_time(schedule: AgentSchedule, ts: datetime) -> bool:
        hour = ts.hour
        start, end = schedule.active_hours
        return start <= hour < end

    @staticmethod
    def _split_tasks(tasks: Iterable[SchedulerTask]) -> tuple[List[SchedulerTask], List[SchedulerTask]]:
        inline: List[SchedulerTask] = []
        isolated: List[SchedulerTask] = []
        for task in tasks:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode == "isolated":
                isolated.append(task)
            else:
                inline.append(task)
        return inline, isolated
