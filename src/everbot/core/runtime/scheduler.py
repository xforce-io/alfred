"""Unified scheduler: heartbeat intervals + cron task dispatch + inspector ticks.

The Scheduler owns **all** scheduling decisions:

- Heartbeat ticks (interval-based, per agent)
- Cron tasks — inline (merged into heartbeat turn) and isolated (dedicated session)
- Inspector ticks (low-frequency observation, default 1h)

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


@dataclass
class InspectorSchedule:
    """Per-agent inspector scheduling state."""

    agent_name: str
    interval_minutes: int = 60  # default 1h
    next_inspect_at: Optional[datetime] = None
    active_hours: tuple[int, int] = (8, 22)
    consecutive_failures: int = 0
    max_backoff_minutes: int = 120


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
        # --- Inspector callback (optional, for inspection tick support) ---
        run_inspector: Optional[Callable[[str, datetime], Awaitable[Any]]] = None,
        # --- Config ---
        agent_schedules: Optional[Dict[str, AgentSchedule]] = None,
        inspector_schedules: Optional[Dict[str, InspectorSchedule]] = None,
        tick_interval_seconds: float = 1.0,
        state_file: Optional[Path] = None,
    ):
        self._get_due_tasks = get_due_tasks
        self._claim_task = claim_task
        self._run_inline = run_inline
        self._run_isolated = run_isolated
        self._run_heartbeat = run_heartbeat
        self._run_inspector = run_inspector
        self._agent_schedules: Dict[str, AgentSchedule] = dict(agent_schedules or {})
        self._inspector_schedules: Dict[str, InspectorSchedule] = dict(inspector_schedules or {})
        self._tick_interval_seconds = tick_interval_seconds
        self._running = False
        self._state_file = state_file
        self._restore_state()

    # -- State persistence --------------------------------------------------

    def _save_state(self) -> None:
        """Persist scheduling timestamps and consecutive_failures to disk."""
        if self._state_file is None:
            return
        try:
            state: Dict[str, Any] = {}
            for name, sched in self._agent_schedules.items():
                entry: Dict[str, Any] = {}
                if sched.next_heartbeat_at is not None:
                    entry["next_heartbeat_at"] = sched.next_heartbeat_at.isoformat()
                entry["consecutive_failures"] = sched.consecutive_failures
                state[name] = entry
            # Inspector schedules
            inspectors: Dict[str, Any] = {}
            for name, isched in self._inspector_schedules.items():
                ientry: Dict[str, Any] = {}
                if isched.next_inspect_at is not None:
                    ientry["next_inspect_at"] = isched.next_inspect_at.isoformat()
                ientry["consecutive_failures"] = isched.consecutive_failures
                inspectors[name] = ientry
            if inspectors:
                state["__inspectors__"] = inspectors
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
            # Restore inspector schedules
            inspector_state = state.get("__inspectors__", {})
            for name, ivalue in inspector_state.items():
                if name not in self._inspector_schedules:
                    continue
                isched = self._inspector_schedules[name]
                if isinstance(ivalue, dict):
                    iso_str = ivalue.get("next_inspect_at")
                    if iso_str:
                        try:
                            isched.next_inspect_at = datetime.fromisoformat(iso_str)
                        except (ValueError, TypeError):
                            pass
                    isched.consecutive_failures = int(ivalue.get("consecutive_failures", 0) or 0)
            logger.debug("Restored scheduler state from %s", self._state_file)
        except Exception:
            logger.debug("Failed to restore scheduler state", exc_info=True)

    # -- Agent schedule management ------------------------------------------

    def register_agent(self, schedule: AgentSchedule) -> None:
        """Register or update an agent's heartbeat schedule."""
        self._agent_schedules[schedule.agent_name] = schedule

    def unregister_agent(self, agent_name: str) -> None:
        self._agent_schedules.pop(agent_name, None)

    def register_inspector(self, schedule: InspectorSchedule) -> None:
        """Register or update an agent's inspector schedule."""
        self._inspector_schedules[schedule.agent_name] = schedule

    def unregister_inspector(self, agent_name: str) -> None:
        self._inspector_schedules.pop(agent_name, None)

    # -- Core tick ----------------------------------------------------------

    async def tick(self, now: Optional[datetime] = None) -> None:
        """Execute one scheduling tick (cron tasks + heartbeats + inspector)."""
        ts = now or datetime.now()

        # Phase 1: Trigger due cron tasks (high frequency, per-minute)
        await self._tick_tasks(ts)

        # Phase 2: Trigger due heartbeat ticks (interval-based)
        await self._tick_heartbeats(ts)

        # Phase 3: Trigger due inspector ticks (low frequency, default 1h)
        await self._tick_inspector(ts)

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

    async def _tick_inspector(self, ts: datetime) -> None:
        """Trigger due inspector ticks (low-frequency observation)."""
        if self._run_inspector is None:
            return
        for schedule in list(self._inspector_schedules.values()):
            if not self._is_active_time_inspector(schedule, ts):
                continue
            if schedule.next_inspect_at is not None and ts < schedule.next_inspect_at:
                continue
            base_interval = max(1, schedule.interval_minutes)
            try:
                await self._run_inspector(schedule.agent_name, ts)
                schedule.consecutive_failures = 0
                schedule.next_inspect_at = ts + timedelta(minutes=base_interval)
            except Exception:
                schedule.consecutive_failures += 1
                backoff_minutes = min(
                    base_interval * (2 ** schedule.consecutive_failures),
                    schedule.max_backoff_minutes,
                )
                schedule.next_inspect_at = ts + timedelta(minutes=backoff_minutes)
                logger.exception(
                    "Inspector tick failed for %s (consecutive_failures=%d, next_retry_in=%d min)",
                    schedule.agent_name,
                    schedule.consecutive_failures,
                    backoff_minutes,
                )
            self._save_state()

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
    def _is_active_time_inspector(schedule: InspectorSchedule, ts: datetime) -> bool:
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
