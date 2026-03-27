"""CronExecutor — scheduled task execution engine.

Extracted from HeartbeatRunner to separate task execution (Cron)
from routine discovery (Inspector). CronExecutor is a pure executor:
it does not decide *when* to run — the Scheduler does.

All HEARTBEAT.md access goes through RoutineManager.
"""

import asyncio
import importlib
import json
import logging
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from ..tasks.execution_gate import TaskExecutionGate
from ..tasks.task_manager import Task, TaskList, TaskState, get_due_tasks
from .cron_delivery import CronDelivery
from .heartbeat_utils import (
    build_isolated_task_prompt,
    build_job_session_id,
    task_snapshot,
    try_deterministic_task,
)

# Whitelist of allowed job module names for dynamic import
ALLOWED_JOBS: frozenset[str] = frozenset({
    "health_check",
    "memory_review",
    "task_discover",
    "skill_evaluate",
})

# Whitelist for HeartbeatRunner._run_isolated_skill().
# Intentionally excludes "skill_evaluate" — that is a cron job, not an
# isolated skill; running it through _run_isolated_skill() would bypass
# the normal cron scheduling and concurrency controls.
ALLOWED_SKILLS: frozenset[str] = frozenset({
    "health_check",
    "memory_review",
    "task_discover",
})

logger = logging.getLogger(__name__)

# Error markers indicating non-retryable (permanent) failures.
_PERMANENT_ERROR_MARKERS: list[str] = [
    "402", "403", "401",
    "insufficient balance", "insufficient_quota", "quota exceeded",
    "rate_limit", "invalid api key", "invalid_api_key",
    "authentication", "authorization", "missing env var",
    "billing", "account deactivated", "access denied",
]


def _is_permanent_error(exc: BaseException) -> bool:
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "status", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )
    if isinstance(status, int) and status in {401, 402, 403}:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_ERROR_MARKERS)


# ── Result types ─────────────────────────────────────────────

@dataclass
class TaskResult:
    """Result of a single task execution."""
    task_id: str
    status: str  # "done" | "failed" | "skipped" | "timeout"
    output: Optional[str] = None
    error: Optional[str] = None
    execution_path: str = ""  # "skill" | "deterministic" | "llm_inline" | "llm_isolated"


@dataclass
class CronTickResult:
    """Summary of one cron tick."""
    executed: int = 0
    skipped: int = 0
    failed: int = 0
    results: List[TaskResult] = field(default_factory=list)

    @property
    def user_visible_output(self) -> str:
        """Aggregate user-visible output from all task results."""
        meaningful = [
            r.output for r in self.results
            if r.output and r.status == "done"
        ]
        return "; ".join(meaningful) if meaningful else "HEARTBEAT_OK"


# ── Status markers (filtered from user output) ───────────────
_STATUS_PREFIXES = (
    "ISOLATED_DONE:", "ISOLATED_TIMEOUT:", "ISOLATED_FAILED:",
    "TASK_FAILED:", "TASK_TIMEOUT:",
    "SKILL_SKIPPED:", "SCANNER_ERROR:",
)


class CronExecutor:
    """Cron task execution engine.

    Executes due tasks from HEARTBEAT.md through three paths:
    - Skill tasks: direct Python module invocation, zero LLM
    - Deterministic tasks: programmatic output, zero LLM
    - LLM tasks: agent-based execution (inline or isolated session)

    All task state operations go through RoutineManager.
    """

    SUMMARY_MAX_CHARS: int = 500

    def __init__(
        self,
        *,
        agent_name: str,
        workspace_path: Path,
        session_manager: Any,
        agent_factory: Callable,
        routine_manager: Any,  # RoutineManager
        delivery: CronDelivery,
        broadcast_scope: str = "agent",
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.routine_manager = routine_manager
        self.delivery = delivery
        self.broadcast_scope = broadcast_scope

    # ── Public API ────────────────────────────────────────────

    async def tick(
        self,
        task_list: TaskList,
        *,
        run_agent: Callable,
        inject_context: Callable,
        agent: Any = None,
        heartbeat_content: str = "",
        run_id: str = "",
        include_inline: bool = True,
        include_isolated: bool = True,
    ) -> CronTickResult:
        """Execute all due tasks in the given task_list.

        Args:
            task_list: In-memory TaskList (already loaded by caller under lock).
            run_agent: async callable(agent, message, **kwargs) -> str
            inject_context: async callable(agent, content, mode, current_task) -> str
            agent: Pre-created LLM agent (for inline LLM tasks).
            heartbeat_content: Raw HEARTBEAT.md content for context injection.
            run_id: Heartbeat run identifier.
            include_inline: Whether to execute inline tasks.
            include_isolated: Whether to execute isolated tasks.
        """
        result = CronTickResult()
        due = get_due_tasks(task_list)

        inline_due = [t for t in due if self._task_mode(t) != "isolated"]
        isolated_due = [t for t in due if self._task_mode(t) == "isolated"]

        if include_inline:
            for task in inline_due:
                tr = await self._execute_inline_task(
                    task, task_list,
                    run_agent=run_agent,
                    inject_context=inject_context,
                    agent=agent,
                    heartbeat_content=heartbeat_content,
                    run_id=run_id,
                )
                result.results.append(tr)
                if tr.status == "done":
                    result.executed += 1
                elif tr.status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1

        if include_isolated:
            for task in isolated_due:
                tr = await self._execute_isolated_task_entry(
                    task, task_list,
                    run_agent=run_agent,
                    run_id=run_id,
                )
                result.results.append(tr)
                if tr.status == "done":
                    result.executed += 1
                elif tr.status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1

        return result

    # ── Scheduler-facing task listing ─────────────────────────

    def list_due_inline_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due inline tasks for external scheduler routing."""
        task_list = self.routine_manager.load_task_list()
        if task_list is None:
            return []
        self.routine_manager.recover_stuck_running_tasks(task_list, now=now)
        due = get_due_tasks(task_list, now=now)
        return [
            task_snapshot(t) for t in due
            if self._task_mode(t) != "isolated" and task_snapshot(t)["id"]
        ]

    def list_due_isolated_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due isolated tasks for external scheduler routing."""
        task_list = self.routine_manager.load_task_list()
        if task_list is None:
            return []
        self.routine_manager.recover_stuck_running_tasks(task_list, now=now)
        due = get_due_tasks(task_list, now=now)
        return [
            task_snapshot(t) for t in due
            if self._task_mode(t) == "isolated" and task_snapshot(t)["id"]
        ]

    async def claim_isolated_task(self, task_id: str, now: Optional[datetime] = None) -> bool:
        """Claim one isolated task with session lock protection."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return False

        session_id = self.session_manager.get_heartbeat_session_id(self.agent_name)
        inproc_acquired = await self.session_manager.acquire_session(session_id, timeout=0.1)
        if not inproc_acquired:
            return False
        try:
            with self.session_manager.file_lock(session_id, blocking=False) as acquired:
                if not acquired:
                    return False
                return self._claim_isolated_task_under_lock(task_id, now=now)
        finally:
            self.session_manager.release_session(session_id)

    def _claim_isolated_task_under_lock(self, task_id: str, now: Optional[datetime] = None) -> bool:
        """Claim one isolated task while lock is held."""
        task_list = self.routine_manager.load_task_list()
        if task_list is None:
            return False
        due = get_due_tasks(task_list, now=now)
        for task in due:
            if self._task_mode(task) != "isolated":
                continue
            if str(getattr(task, "id", "")) != task_id:
                continue
            if not self.routine_manager.claim_task(task, now=now):
                return False
            self.routine_manager.flush(task_list)
            return True
        return False

    async def execute_isolated_claimed_task(
        self,
        task_snapshot_dict: dict[str, Any],
        *,
        run_agent: Callable,
        run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Execute one already-claimed isolated task and persist final state."""
        task_id = str(task_snapshot_dict.get("id") or "").strip()
        if not task_id:
            return
        try:
            task = Task.from_dict(task_snapshot_dict)
            task.execution_mode = "isolated"

            # Gate check for job tasks
            verdict = None
            if task.job:
                gate = TaskExecutionGate(self.workspace_path, self.agent_name, self._get_scanner)
                verdict = gate.check(task)
                if not verdict.allowed:
                    self._write_event("job_skipped", skill=task.job, reason=verdict.skip_reason)
                    await self._update_isolated_task_state(task_id, TaskState.DONE, now=now)
                    return

            active_run_id = run_id or f"heartbeat_isolated_{uuid.uuid4().hex[:12]}"
            await self._run_isolated_task(task, active_run_id, run_agent=run_agent)

            if task.job and verdict and verdict.scan_result:
                gate.commit(task, verdict)

            await self._update_isolated_task_state(task_id, TaskState.DONE, now=now)
        except Exception as exc:
            await self._update_isolated_task_state(
                task_id, TaskState.FAILED, error_message=str(exc), now=now,
            )
            raise

    # ── Internal: inline task execution ───────────────────────

    async def _execute_inline_task(
        self,
        task: Task,
        task_list: TaskList,
        *,
        run_agent: Callable,
        inject_context: Callable,
        agent: Any,
        heartbeat_content: str,
        run_id: str,
    ) -> TaskResult:
        """Execute one inline task (skill, deterministic, or LLM)."""
        if not self.routine_manager.claim_task(task):
            return TaskResult(task_id=task.id, status="skipped", execution_path="claim_failed")

        self.routine_manager.flush(task_list)
        self._write_event("task_start", task_id=task.id, title=task.title, execution_mode="inline")

        try:
            # 1. Job tasks: gate check + job module, no LLM
            if task.job:
                return await self._execute_job_task(task, task_list, run_id)

            # 2. Deterministic tasks: programmatic output
            deterministic_result = try_deterministic_task(task)
            if deterministic_result is not None:
                self.routine_manager.update_task_state(task, TaskState.DONE)
                self._write_event("task_done", task_id=task.id, title=task.title)
                self.routine_manager.flush(task_list)
                return TaskResult(
                    task_id=task.id, status="done",
                    output=deterministic_result, execution_path="deterministic",
                )

            # 3. LLM inline task
            user_message = await inject_context(agent, heartbeat_content, "execute_due", task)
            result = await asyncio.wait_for(
                run_agent(agent, user_message),
                timeout=task.timeout_seconds,
            )
            self.routine_manager.update_task_state(task, TaskState.DONE)
            self._write_event("task_done", task_id=task.id, title=task.title)
            self.routine_manager.flush(task_list)
            return TaskResult(
                task_id=task.id, status="done",
                output=result, execution_path="llm_inline",
            )

        except asyncio.TimeoutError:
            self.routine_manager.update_task_state(task, TaskState.FAILED, error_message="timeout")
            self._write_event("task_failed", task_id=task.id, title=task.title, error="timeout")
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="timeout", error="timeout")

        except Exception as exc:
            self.routine_manager.update_task_state(task, TaskState.FAILED, error_message=str(exc))
            self._write_event("task_failed", task_id=task.id, title=task.title, error=str(exc)[:200])
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="failed", error=str(exc))

    async def _execute_job_task(self, task: Task, task_list: TaskList, run_id: str) -> TaskResult:
        """Execute a job task with gate check."""
        gate = TaskExecutionGate(self.workspace_path, self.agent_name, self._get_scanner)
        verdict = gate.check(task)

        if verdict.scan_result is not None:
            self._write_event(
                "scanner_check", scanner=task.scanner,
                has_changes=verdict.scan_result.has_changes,
                change_summary=verdict.scan_result.change_summary,
            )

        if not verdict.allowed:
            if verdict.skip_reason == "scanner_error":
                self._write_event("scanner_error", scanner=task.scanner, error="gate check failed")
            else:
                self._write_event("job_skipped", skill=task.job, reason=verdict.skip_reason)
            self._rearm_job_task(task)
            self.routine_manager.flush(task_list)
            return TaskResult(
                task_id=task.id, status="skipped",
                execution_path="skill",
                error=verdict.skip_reason,
            )

        result = await asyncio.wait_for(
            self._invoke_job(task, verdict.scan_result, run_id),
            timeout=task.timeout_seconds,
        )
        if verdict.scan_result:
            gate.commit(task, verdict)
        self._rearm_job_task(task)
        self._write_event("task_done", task_id=task.id, title=task.title)
        self.routine_manager.flush(task_list)
        return TaskResult(
            task_id=task.id, status="done",
            output=result, execution_path="skill",
        )

    # ── Internal: isolated task execution ─────────────────────

    async def _execute_isolated_task_entry(
        self,
        task: Task,
        task_list: TaskList,
        *,
        run_agent: Callable,
        run_id: str,
    ) -> TaskResult:
        """Execute one isolated task from the due list."""
        if not self.routine_manager.claim_task(task):
            return TaskResult(task_id=task.id, status="skipped", execution_path="claim_failed")

        # Gate check for job tasks before dispatching
        verdict = None
        if task.job:
            gate = TaskExecutionGate(self.workspace_path, self.agent_name, self._get_scanner)
            verdict = gate.check(task)
            if verdict.scan_result is not None:
                self._write_event(
                    "scanner_check", scanner=task.scanner,
                    has_changes=verdict.scan_result.has_changes,
                    change_summary=verdict.scan_result.change_summary,
                )
            if not verdict.allowed:
                if verdict.skip_reason == "scanner_error":
                    self._write_event("scanner_error", scanner=task.scanner, error="gate check failed")
                else:
                    self._write_event("job_skipped", skill=task.job, reason=verdict.skip_reason)
                self._rearm_job_task(task)
                self.routine_manager.flush(task_list)
                return TaskResult(
                    task_id=task.id, status="skipped",
                    execution_path="skill", error=verdict.skip_reason,
                )

        self.routine_manager.flush(task_list)
        self._write_event("task_start", task_id=task.id, title=task.title, execution_mode="isolated")

        try:
            await self._run_isolated_task(task, run_id, run_agent=run_agent)
            if verdict and verdict.scan_result:
                gate.commit(task, verdict)
            self.routine_manager.update_task_state(task, TaskState.DONE)
            self._write_event("task_done", task_id=task.id, title=task.title)
            self.routine_manager.flush(task_list)
            return TaskResult(
                task_id=task.id, status="done",
                output=f"ISOLATED_DONE:{task.id}", execution_path="llm_isolated",
            )
        except asyncio.TimeoutError:
            self.routine_manager.update_task_state(task, TaskState.FAILED, error_message="timeout")
            self._write_event("task_failed", task_id=task.id, title=task.title, error="timeout")
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="timeout", error="timeout", execution_path="llm_isolated")
        except Exception as exc:
            self.routine_manager.update_task_state(task, TaskState.FAILED, error_message=str(exc))
            self._write_event("task_failed", task_id=task.id, title=task.title, error=str(exc)[:200])
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="failed", error=str(exc), execution_path="llm_isolated")

    async def _run_isolated_task(self, task: Task, run_id: str, *, run_agent: Callable) -> str:
        """Execute one isolated task with a dedicated job session."""
        job_session_id = build_job_session_id(task)
        task_title = str(task.title or "")
        prompt = build_isolated_task_prompt(task)

        agent = await self._create_job_agent(job_session_id)
        job_system_prompt = self._build_job_system_prompt(agent, task)
        try:
            result = await asyncio.wait_for(
                run_agent(agent, prompt, system_prompt_override=job_system_prompt),
                timeout=task.timeout_seconds,
            )
            await self.session_manager.save_session(job_session_id, agent)
            await self.session_manager.mark_session_archived(job_session_id)

            summary = f"{task_title or task.id} completed"
            await self.delivery.deposit_job_event(
                event_type="job_completed",
                source_session_id=job_session_id,
                summary=summary,
                detail=result,
                run_id=run_id,
            )
            await self.delivery.inject_to_history(result, run_id)
            await self.delivery._emit_realtime(result, run_id)
            return result
        except Exception as exc:
            await self.session_manager.save_session(job_session_id, agent)
            await self.session_manager.mark_session_archived(job_session_id)

            summary = f"{task_title or task.id} failed"
            await self.delivery.deposit_job_event(
                event_type="job_failed",
                source_session_id=job_session_id,
                summary=summary,
                detail=str(exc),
                run_id=run_id,
            )
            from ..tasks.task_manager import format_retry_hint
            retry_hint = format_retry_hint(task)
            fail_msg = f"Task failed: {task_title or task.id}\n{exc}"
            if retry_hint:
                fail_msg += f"\n\n{retry_hint}"
            await self.delivery._emit_realtime(fail_msg, run_id)
            raise

    async def _create_job_agent(self, job_session_id: str) -> Any:
        """Create a fresh agent for isolated job execution."""
        from ...infra.user_data import get_user_data_manager

        agent = await self.agent_factory(self.agent_name, self.workspace_path)
        context = agent.executor.context
        context.set_variable("session_id", job_session_id)
        if hasattr(context, "set_session_id"):
            context.set_session_id(job_session_id)
        context.set_variable("job_session_id", job_session_id)
        user_data = get_user_data_manager()
        trajectory_path = user_data.get_session_trajectory_path(self.agent_name, job_session_id)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        context.init_trajectory(str(trajectory_path), overwrite=True)
        return agent

    @staticmethod
    def _build_job_system_prompt(agent: Any, task: Task) -> str:
        """Build isolated job system prompt from base workspace + task description."""
        context = agent.executor.context
        base = ""
        if hasattr(context, "workspace_instructions"):
            base = str(context.workspace_instructions or "")
        elif hasattr(context, "get_variable"):
            base = str(context.get_variable("workspace_instructions") or "")
        task_description = str(task.description or "").strip()
        if not task_description:
            return base
        if not base:
            return task_description
        return f"{base}\n\n{task_description}"

    # ── Job execution ──────────────────────────────────────────

    async def _invoke_job(self, task: Task, scan_result: Any, run_id: str) -> str:
        """Execute a cron job task."""
        job_name = task.job
        start_ms = int(_time.time() * 1000)

        self._write_event(
            "job_started", skill=job_name,
            scan_summary=scan_result.change_summary if scan_result else "",
        )

        try:
            context = self._build_job_context(scan_result)
            module_name = job_name.replace("-", "_")
            if module_name not in ALLOWED_JOBS:
                raise ValueError(f"Job {job_name!r} is not in the allowed jobs whitelist")
            _pkg = __name__.rsplit(".", 2)[0]  # e.g. "src.everbot.core"
            try:
                job_module = importlib.import_module(f"{_pkg}.jobs.{module_name}")
            except ModuleNotFoundError as e:
                raise RuntimeError(
                    f"Cannot import job module '{module_name}': {e}. "
                    f"Ensure daemon runs from project root or package is installed."
                ) from e
            result = await job_module.run(context)

            duration_ms = int(_time.time() * 1000) - start_ms
            self._write_event(
                "job_completed", skill=job_name,
                duration_ms=duration_ms, result=str(result)[:200],
            )
            return str(result)
        except Exception as exc:
            duration_ms = int(_time.time() * 1000) - start_ms
            self._write_event(
                "job_failed", skill=job_name,
                duration_ms=duration_ms, error=str(exc)[:200],
            )
            logger.error("Job %s failed: %s", job_name, exc, exc_info=True)
            raise

    def _build_job_context(self, scan_result=None):
        """Build SkillContext for job execution."""
        from .skill_context import SkillContext, MailboxAdapter
        from ..memory.manager import MemoryManager
        from ...infra.user_data import get_user_data_manager

        memory_path = self.workspace_path / "MEMORY.md"
        user_data = get_user_data_manager()
        primary_session_id = self.session_manager.get_primary_session_id(self.agent_name)
        return SkillContext(
            sessions_dir=user_data.sessions_dir,
            workspace_path=self.workspace_path,
            agent_name=self.agent_name,
            memory_manager=MemoryManager(memory_path),
            mailbox=MailboxAdapter(self.session_manager, primary_session_id, self.agent_name),
            llm=_SkillLLMClient(),
            scan_result=scan_result,
        )

    def _get_scanner(self, scanner_type: Optional[str]) -> Optional[Any]:
        """Get scanner instance by type name."""
        if not scanner_type:
            return None
        if scanner_type == "session":
            from ..scanners.session_scanner import SessionScanner
            from ...infra.user_data import get_user_data_manager
            user_data = get_user_data_manager()
            return SessionScanner(user_data.sessions_dir)
        logger.warning("Unknown scanner type: %s", scanner_type)
        return None

    @staticmethod
    def _rearm_job_task(task: Task) -> None:
        """Re-arm a job task to PENDING for next scan cycle."""
        from ..tasks.task_manager import update_task_state, TaskState
        update_task_state(task, TaskState.DONE)

    # ── Isolated task state management (lock-protected) ───────

    async def _update_isolated_task_state(
        self,
        task_id: str,
        state: TaskState,
        *,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Update one isolated task state under lock and flush file."""
        from ..tasks.task_manager import update_task_state

        session_id = self.session_manager.get_heartbeat_session_id(self.agent_name)
        max_retries = 3
        for attempt in range(max_retries):
            inproc_acquired = await self.session_manager.acquire_session(session_id, timeout=5.0)
            if not inproc_acquired:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"Failed to acquire session lock for task {task_id}")
            try:
                with self.session_manager.file_lock(session_id, blocking=True) as acquired:
                    if not acquired:
                        if attempt < max_retries - 1:
                            continue
                        raise RuntimeError(f"Failed to acquire file lock for task {task_id}")
                    # Re-read and apply state change
                    task_list = self.routine_manager.load_task_list()
                    if task_list is None:
                        return
                    for task in task_list.tasks:
                        if str(getattr(task, "id", "")) != task_id:
                            continue
                        update_task_state(task, state, error_message=error_message, now=now)
                        self.routine_manager.flush(task_list)
                        return
                    return
            finally:
                self.session_manager.release_session(session_id)

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _task_mode(task: Any) -> str:
        return str(getattr(task, "execution_mode", "inline") or "inline")

    def _write_event(self, event_type: str, **kwargs: Any) -> None:
        """Write a structured event to the JSONL events file."""
        try:
            from ...infra.user_data import get_user_data_manager
            user_data = get_user_data_manager()
            events_file = user_data.heartbeat_events_file
            events_file.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "timestamp": datetime.now().isoformat(),
                "agent": self.agent_name,
                "source": "cron",
                "event": event_type,
            }
            event.update(kwargs)
            with open(events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("Failed to write cron event: %s", exc)


class _SkillLLMClient:
    """Lightweight LLM client for reflection skills."""

    def __init__(self, model: str = ""):
        self._model = model

    async def complete(self, prompt: str, system: str = "") -> str:
        try:
            import litellm
        except ImportError as e:
            raise RuntimeError("litellm is required for skill LLM calls") from e

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        model = self._model
        if not model:
            import os
            model = os.environ.get("ALFRED_SKILL_MODEL", "deepseek/deepseek-chat")

        response = await litellm.acompletion(
            model=model, messages=messages,
            temperature=0.3, max_tokens=2000,
        )
        return response.choices[0].message.content or ""
