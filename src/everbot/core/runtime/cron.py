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
import re
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from ..jobs.llm_errors import LLMConfigError, LLMTransientError
from ..tasks.execution_gate import GateVerdict, TaskExecutionGate
from ..tasks.task_manager import (
    RetryDecision,
    Task,
    TaskList,
    TaskState,
    build_retry_decision,
    format_retry_hint,
    get_due_tasks,
)
from .cron_delivery import CronDelivery
from .routine_checkpoint import (
    RoutineCheckpointStore,
    build_delivery_key,
    content_hash,
)
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


def _is_retryable_error(exc: BaseException) -> bool:
    structured = getattr(exc, "retryable", None)
    if isinstance(structured, bool):
        return structured
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if _is_permanent_error(exc):
        return False
    from .heartbeat import _is_transient_llm_error
    if _is_transient_llm_error(exc):
        return True
    return True


def _decision_for_failure(task: Task, exc: BaseException) -> RetryDecision:
    existing = getattr(exc, "_routine_retry_decision", None)
    if isinstance(existing, RetryDecision):
        return existing
    return build_retry_decision(task, retryable=_is_retryable_error(exc))


def _attach_retry_decision(exc: BaseException, decision: RetryDecision) -> None:
    try:
        setattr(exc, "_routine_retry_decision", decision)
    except Exception:
        pass



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
    def user_visible_output(self) -> Optional[str]:
        """Aggregate user-visible output from all task results.

        Jobs returning None are silent (e.g. routine bookkeeping with no
        actionable content) and contribute nothing to the aggregate. When
        every job is silent the property returns None, signalling the
        caller to suppress delivery entirely.
        """
        meaningful = [
            r.output for r in self.results
            if r.output is not None and r.status == "done"
        ]
        return "; ".join(meaningful) if meaningful else None


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
        active_hours: tuple[int, int] = (0, 24),
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.routine_manager = routine_manager
        self.delivery = delivery
        scope = str(broadcast_scope or "agent").strip().lower()
        self.broadcast_scope = broadcast_scope
        self.active_hours = active_hours

        # SLM: record skill invocations for evaluation (per-agent isolation)
        try:
            from ...infra.user_data import get_user_data_manager as _get_udm
            _udm = _get_udm()
            self._skill_log_recorder: Any = _udm.get_skill_log_recorder(
                agent_name=agent_name,
                workspace_path=workspace_path,
            )
        except Exception:
            self._skill_log_recorder = None

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

    def _resolve_skill_model(self) -> str:
        """Resolve the model for skill LLM calls.

        Uses the 'fast' model from models.yaml (model_config) — skill jobs
        (eval, reflection) are simple scoring tasks that don't need
        a reasoning model.  Falls back to agent model if 'fast' is unset.
        """
        from ..agent.provider.model_config import load_model_config
        from ..agent.agent_config import resolve_agent_model
        try:
            fast_model = load_model_config().fast_model
            if fast_model:
                return fast_model
        except Exception:
            pass
        return resolve_agent_model(self.agent_name)

    # ── Scheduler-facing task listing ─────────────────────────

    def list_due_inline_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due inline tasks for external scheduler routing.

        Filters out tasks whose min_execution_interval hasn't elapsed,
        preventing the scheduler from spinning on gated tasks.
        """
        task_list = self.routine_manager.load_task_list()
        if task_list is None:
            return []
        self.routine_manager.recover_stuck_running_tasks(task_list, now=now)
        due = get_due_tasks(task_list, now=now)
        return [
            task_snapshot(t) for t in due
            if self._task_mode(t) != "isolated"
            and task_snapshot(t)["id"]
            and TaskExecutionGate._check_min_execution_interval(t, now=now)
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

            # Skip isolated job tasks outside active hours
            if task.job and not self._is_active_hour():
                self._write_event("job_skipped", skill=task.job, reason="outside_active_hours")
                await self._update_isolated_task_state(task_id, TaskState.DONE, now=now)
                return

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
        # Gate check BEFORE claim: claim_task sets last_run_at=now (RUNNING
        # state), which would make the gate's min_execution_interval check
        # always fail.  Running the gate first sees the real previous run time.
        if task.job:
            if not self._is_active_hour():
                self._write_event("job_skipped", skill=task.job, reason="outside_active_hours")
                self._rearm_job_task(task)
                self.routine_manager.flush(task_list)
                return TaskResult(
                    task_id=task.id, status="skipped",
                    execution_path="skill",
                    error="outside_active_hours",
                )
            verdict = self._check_job_gate(task)
            self._emit_gate_events(task, verdict)
            if not verdict.allowed:
                # Rearm next_run_at so the scheduler doesn't re-trigger
                # this task every tick (prevents spin on gated tasks).
                self._rearm_job_task(task)
                self.routine_manager.flush(task_list)
                return TaskResult(
                    task_id=task.id, status="skipped",
                    execution_path="skill",
                    error=verdict.skip_reason,
                )

        if not self.routine_manager.claim_task(task):
            return TaskResult(task_id=task.id, status="skipped", execution_path="claim_failed")

        self.routine_manager.flush(task_list)
        self._write_event("task_start", task_id=task.id, title=task.title, execution_mode="inline")

        try:
            # 1. Job tasks: already passed gate check above
            if task.job:
                return await self._execute_job_task(task, task_list, run_id, verdict=verdict)

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
            decision = build_retry_decision(task, retryable=True)
            self.routine_manager.update_task_state(
                task, TaskState.FAILED, error_message="timeout",
                retryable=True, error_code="TIMEOUT", retry_decision=decision,
            )
            self._write_event(
                "task_failed", task_id=task.id, title=task.title, error="timeout",
                retryable=True, retry_number=decision.retry_number,
                max_retry=decision.max_retry, next_run_at=decision.next_run_at,
            )
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="timeout", error="timeout")

        except Exception as exc:
            decision = _decision_for_failure(task, exc)
            self.routine_manager.update_task_state(
                task, TaskState.FAILED, error_message=str(exc),
                retryable=_is_retryable_error(exc),
                error_code=getattr(exc, "code", None),
                retry_decision=decision,
            )
            self._write_event(
                "task_failed", task_id=task.id, title=task.title, error=str(exc)[:200],
                error_code=getattr(exc, "code", None),
                retryable=_is_retryable_error(exc),
                retry_number=decision.retry_number,
                max_retry=decision.max_retry,
                next_run_at=decision.next_run_at,
            )
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="failed", error=str(exc))

    def _check_job_gate(self, task: Task) -> GateVerdict:
        """Run gate check for a job task (scanner + min_execution_interval)."""
        gate = TaskExecutionGate(self.workspace_path, self.agent_name, self._get_scanner)
        return gate.check(task)

    def _emit_gate_events(self, task: Task, verdict: GateVerdict) -> None:
        """Write heartbeat events for gate check results."""
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

    async def _execute_job_task(
        self,
        task: Task,
        task_list: TaskList,
        run_id: str,
        *,
        verdict: GateVerdict,
    ) -> TaskResult:
        """Execute a job task whose gate check already passed."""
        result = await asyncio.wait_for(
            self._invoke_job(task, verdict.scan_result, run_id),
            timeout=task.timeout_seconds,
        )
        if verdict.scan_result:
            TaskExecutionGate(
                self.workspace_path, self.agent_name, self._get_scanner,
            ).commit(task, verdict)
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
        # Gate check BEFORE claim (same rationale as inline path)
        verdict = None
        if task.job:
            if not self._is_active_hour():
                self._write_event("job_skipped", skill=task.job, reason="outside_active_hours")
                return TaskResult(
                    task_id=task.id, status="skipped",
                    execution_path="skill", error="outside_active_hours",
                )
            verdict = self._check_job_gate(task)
            self._emit_gate_events(task, verdict)
            if not verdict.allowed:
                return TaskResult(
                    task_id=task.id, status="skipped",
                    execution_path="skill", error=verdict.skip_reason,
                )

        if not self.routine_manager.claim_task(task):
            return TaskResult(task_id=task.id, status="skipped", execution_path="claim_failed")

        self.routine_manager.flush(task_list)
        self._write_event("task_start", task_id=task.id, title=task.title, execution_mode="isolated")

        exec_path = "job_isolated" if task.job else "llm_isolated"
        try:
            await self._run_isolated_task(task, run_id, run_agent=run_agent)
            if verdict and verdict.scan_result:
                TaskExecutionGate(
                    self.workspace_path, self.agent_name, self._get_scanner,
                ).commit(task, verdict)
            self.routine_manager.update_task_state(task, TaskState.DONE)
            self._write_event("task_done", task_id=task.id, title=task.title)
            self.routine_manager.flush(task_list)
            return TaskResult(
                task_id=task.id, status="done",
                output=f"ISOLATED_DONE:{task.id}", execution_path=exec_path,
            )
        except asyncio.TimeoutError as exc:
            decision = _decision_for_failure(task, exc)
            self.routine_manager.update_task_state(
                task, TaskState.FAILED, error_message="timeout",
                retryable=True, error_code="TIMEOUT", retry_decision=decision,
            )
            self._write_event(
                "task_failed", task_id=task.id, title=task.title, error="timeout",
                error_code="TIMEOUT", retryable=True,
                retry_number=decision.retry_number, max_retry=decision.max_retry,
                next_run_at=decision.next_run_at,
            )
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="timeout", error="timeout", execution_path=exec_path)
        except Exception as exc:
            decision = _decision_for_failure(task, exc)
            retryable = _is_retryable_error(exc)
            self.routine_manager.update_task_state(
                task, TaskState.FAILED, error_message=str(exc),
                retryable=retryable,
                error_code=getattr(exc, "code", None),
                retry_decision=decision,
            )
            self._write_event(
                "task_failed", task_id=task.id, title=task.title, error=str(exc)[:200],
                error_code=getattr(exc, "code", None), retryable=retryable,
                retry_number=decision.retry_number, max_retry=decision.max_retry,
                next_run_at=decision.next_run_at,
            )
            self.routine_manager.flush(task_list)
            return TaskResult(task_id=task.id, status="failed", error=str(exc), execution_path=exec_path)

    async def _run_isolated_task(self, task: Task, run_id: str, *, run_agent: Callable) -> Optional[str]:
        """Execute one isolated task.

        Routes by task type:
        - job tasks (task.job set): call _invoke_job() → Python module
        - agent tasks (no job): create agent session → LLM turn

        Both paths use the delivery pipeline for result delivery. Job
        tasks may return None to indicate silent completion.
        """
        if task.job:
            return await self._run_isolated_job(task, run_id)
        return await self._run_isolated_agent(task, run_id, run_agent=run_agent)

    async def _run_isolated_job(self, task: Task, run_id: str) -> Optional[str]:
        """Execute an isolated job task via Python module, with delivery.

        Silent jobs (return value None) skip every user-facing channel —
        no mailbox event, no history injection, no realtime push. The
        timeline ``task_done`` event written by the caller still records
        the run for observability.
        """
        task_title = str(task.title or "")
        scan_result = None  # isolated jobs don't carry scan_result from gate
        try:
            result = await asyncio.wait_for(
                self._invoke_job(task, scan_result, run_id),
                timeout=task.timeout_seconds,
            )

            if result is None:
                return None

            summary = f"{task_title or task.id} completed"
            job_session_id = f"job_{task.id}"
            await self.delivery.deposit_job_event(
                event_type="job_completed",
                source_session_id=job_session_id,
                summary=summary,
                detail=result,
                run_id=run_id,
            )
            await self.delivery.inject_to_history(result, run_id)
            await self.delivery._emit_realtime(
                result, run_id, transcript_worthy=True,
                source_session_id=job_session_id,  # #122:可解析溯源锚点,非合成 run_id
            )

            return result
        except Exception as exc:
            summary = f"{task_title or task.id} failed"
            await self.delivery.deposit_job_event(
                event_type="job_failed",
                source_session_id=f"job_{task.id}",
                summary=summary,
                detail=str(exc),
                run_id=run_id,
            )
            decision = build_retry_decision(task, retryable=_is_retryable_error(exc))
            _attach_retry_decision(exc, decision)
            retry_hint = format_retry_hint(task, decision)
            fail_msg = f"Task failed: {task_title or task.id}\n{exc}"
            if retry_hint:
                fail_msg += f"\n\n{retry_hint}"
            await self.delivery._emit_realtime(fail_msg, run_id)
            raise

    async def _run_isolated_agent(self, task: Task, run_id: str, *, run_agent: Callable) -> str:
        """Execute an isolated agent task with a dedicated LLM session."""
        job_session_id = build_job_session_id(task)
        task_title = str(task.title or "")
        prompt = build_isolated_task_prompt(task)

        agent = await self._create_job_agent(job_session_id)
        job_system_prompt = self._build_job_system_prompt(agent, task)
        checkpoint_store: Optional[RoutineCheckpointStore] = None
        try:
            staged = getattr(task, "staged", None)
            if staged:
                result, checkpoint_store = await self._run_staged_agent(
                    task, agent, run_id, run_agent=run_agent,
                    system_prompt=job_system_prompt,
                )
            else:
                result = await asyncio.wait_for(
                    run_agent(agent, prompt, system_prompt_override=job_system_prompt),
                    timeout=task.timeout_seconds,
                )
            await self.session_manager.save_session(job_session_id, agent)
            await self.session_manager.mark_session_archived(job_session_id)

            # #127 L1 provenance gate (observe-only): log whether this report run
            # was backed by real tools + cites. Does NOT alter delivery — we
            # measure the live flag rate before any banner/block (防误杀).
            self._observe_provenance(task, agent)

            # #130 T1: mechanically append each signal's top-1 source link to the
            # delivered result (independent of the LLM prose).
            result = self._append_run_provenance(result, agent)

            # #130 T2: the projection anchor must be the milkie run id (deref-able by the
            # consuming agent via get_execution/get_lineage under milkie#200's
            # delivered-runId allowlist), not the job session id — readByRunId is keyed by
            # the milkie runId. Fall back to the session id when no run id was captured.
            projection_anchor = getattr(agent, "last_run_id", None) or job_session_id

            summary = f"{task_title or task.id} completed"
            if checkpoint_store is None:
                await self.delivery.deposit_job_event(
                    event_type="job_completed",
                    source_session_id=job_session_id,
                    summary=summary,
                    detail=result,
                    run_id=run_id,
                )
                await self.delivery.inject_to_history(result, run_id)
                await self.delivery._emit_realtime(
                    result, run_id, transcript_worthy=True,
                    source_session_id=projection_anchor,  # #130 T2: milkie runId, deref-able
                )
            else:
                destination = str(getattr(task, "staged", {}).get("destination") or "primary")
                delivery_key = build_delivery_key(task.execution_id, result, destination)
                await checkpoint_store.run_delivery_step(
                    "mailbox", delivery_key,
                    lambda: self.delivery.deposit_job_event(
                        event_type="job_completed",
                        source_session_id=job_session_id,
                        summary=summary,
                        detail=result,
                        run_id=run_id,
                    ),
                )
                await checkpoint_store.run_delivery_step(
                    "history", delivery_key,
                    lambda: self.delivery.inject_to_history(result, run_id),
                )
                await checkpoint_store.run_delivery_step(
                    "realtime", delivery_key,
                    lambda: self.delivery._emit_realtime(
                        result, run_id, transcript_worthy=True,
                        source_session_id=projection_anchor,
                    ),
                )

            # SLM: record skill invocations from this isolated agent run.
            # Reads the just-written trajectory to find _load_resource_skill
            # calls and writes one segment per unique skill into skill_logs/.
            self._record_skill_log(task, result, job_session_id)

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
            decision = build_retry_decision(task, retryable=_is_retryable_error(exc))
            _attach_retry_decision(exc, decision)
            retry_hint = format_retry_hint(task, decision)
            fail_msg = f"Task failed: {task_title or task.id}\n{exc}"
            if retry_hint:
                fail_msg += f"\n\n{retry_hint}"
            await self.delivery._emit_realtime(fail_msg, run_id)
            raise

    async def _run_staged_agent(
        self,
        task: Task,
        agent: Any,
        run_id: str,
        *,
        run_agent: Callable,
        system_prompt: str,
    ) -> tuple[str, RoutineCheckpointStore]:
        """Run the fixed fetch/analyze shape and reuse valid durable artifacts."""
        staged = getattr(task, "staged", None)
        if not isinstance(staged, dict):
            raise ValueError("staged routine descriptor must be an object")
        execution_id = str(getattr(task, "execution_id", "") or "")
        if not execution_id:
            raise ValueError("staged routine requires a claimed execution identity")

        prompts: dict[str, str] = {}
        for name in ("fetch", "analyze"):
            stage = staged.get(name)
            prompt = stage.get("prompt") if isinstance(stage, dict) else None
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"staged routine requires {name}.prompt")
            prompts[name] = prompt.strip()

        store = RoutineCheckpointStore(self.workspace_path, execution_id, task.id)

        async def fetch() -> str:
            return await asyncio.wait_for(
                run_agent(agent, prompts["fetch"], system_prompt_override=system_prompt),
                timeout=task.timeout_seconds,
            )

        fetched = await store.run_stage(
            "fetch", prompts["fetch"], fetch,
            run_id=getattr(agent, "last_run_id", None) or run_id,
        )
        analyze_prompt = f"{prompts['analyze']}\n\nFetch artifact:\n{fetched}"

        async def analyze() -> str:
            return await asyncio.wait_for(
                run_agent(agent, analyze_prompt, system_prompt_override=system_prompt),
                timeout=task.timeout_seconds,
            )

        analyzed = await store.run_stage(
            "analyze",
            f"{prompts['analyze']}\0{content_hash(fetched)}",
            analyze,
            run_id=getattr(agent, "last_run_id", None) or run_id,
        )
        return analyzed, store

    def _observe_provenance(self, task: Task, agent: Any) -> None:
        """#127 L1 (observe-only): log a backing verdict for this isolated report
        run — did it run real data tools, did it cite? LOG ONLY; never alters
        delivery and never raises into it. Lets us measure the live flag rate
        before turning on any banner/block (防误杀: observe-first)."""
        try:
            from .provenance_gate import assess_report_backing, read_run_events
            from ..agent.provider.milkie.provider import _milkie_data_dir

            milkie_run_id = getattr(agent, "last_run_id", None)
            if not milkie_run_id:
                return
            events = read_run_events(_milkie_data_dir(self.agent_name), milkie_run_id)
            if not events:
                return
            v = assess_report_backing(events)
            verdict = (
                "UNBACKED" if not v.has_tool_backing
                else "NO_CITES" if not v.has_cites
                else "ok"
            )
            logger.info(
                "[provenance/observe] task=%s run=%s tool_calls=%d cites=%d verdict=%s",
                task.id, milkie_run_id, v.tool_calls, v.cites, verdict,
            )
        except Exception:
            logger.debug("provenance observe failed", exc_info=True)

    def _append_run_provenance(self, result: str, agent: Any) -> str:
        """#130 T1: mechanically append each signal's top-1 source link to the report —
        independent of the LLM prose (which may drop the links). Source is the trusted
        report-script's PROVENANCE block in the run events. Any failure is swallowed and the
        body returned unchanged (delivery must not crash on the provenance add-on)."""
        try:
            from .provenance_footer import append_provenance_footer
            from .provenance_gate import read_run_events
            from ..agent.provider.milkie.provider import _milkie_data_dir

            run_id = getattr(agent, "last_run_id", None)
            if not run_id:
                return result
            events = read_run_events(_milkie_data_dir(self.agent_name), run_id)
            return append_provenance_footer(result, events)
        except Exception:
            logger.debug("provenance footer failed", exc_info=True)
            return result

    async def _create_job_agent(self, job_session_id: str) -> Any:
        """Create a fresh agent for isolated job execution."""
        from ...infra.user_data import get_user_data_manager

        from ..agent.provider import get_provider_for_agent, provider_for

        # Route creation through the per-agent provider (milkie/dolphin
        # selection). No tools_override → full tool access, matching the
        # isolated-job design (raw factory previously bypassed routing).
        agent = await get_provider_for_agent(self.agent_name).create_agent(
            self.agent_name, self.workspace_path
        )
        provider = provider_for(agent)
        provider.set_session_id(agent, job_session_id)
        provider.set_variable(agent, "job_session_id", job_session_id)
        user_data = get_user_data_manager()
        trajectory_path = user_data.get_session_trajectory_path(self.agent_name, job_session_id)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        provider.init_trajectory(agent, str(trajectory_path), overwrite=True)
        return agent

    @staticmethod
    def _build_job_system_prompt(agent: Any, task: Task) -> str:
        """Build isolated job system prompt from base workspace + task description."""
        from ..agent.provider import provider_for

        provider = provider_for(agent)
        base = ""
        if provider.needs_history_restore():
            # dolphin: 进程内 context,行为保持不变(优先属性,回退 get_variable)
            context = agent.executor.context
            if hasattr(context, "workspace_instructions"):
                base = str(context.workspace_instructions or "")
            elif hasattr(context, "get_variable"):
                base = str(context.get_variable("workspace_instructions") or "")
        else:
            # milkie: 无 .executor;workspace_instructions 走 serve /context/get(可能为 None)
            base = str(provider.get_variable(agent, "workspace_instructions") or "")
        task_description = str(task.description or "").strip()
        if not task_description:
            return base
        if not base:
            return task_description
        return f"{base}\n\n{task_description}"

    # ── Job execution ──────────────────────────────────────────

    async def _invoke_job(self, task: Task, scan_result: Any, run_id: str) -> Optional[str]:
        """Execute a cron job task.

        Returns the job's user-visible summary, or None when the job has
        no user-facing output to surface (silent bookkeeping). Subscribers
        to this return value (inline aggregator, isolated delivery path)
        treat None as "do not surface to user".
        """
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
            return None if result is None else str(result)
        except (LLMTransientError, LLMConfigError) as exc:
            duration_ms = int(_time.time() * 1000) - start_ms
            self._write_event(
                "job_degraded", skill=job_name,
                duration_ms=duration_ms, error=str(exc)[:200],
                retriable=isinstance(exc, LLMTransientError),
            )
            logger.warning("Job %s skipped (LLM unavailable): %s", job_name, exc)
            return f"LLM unavailable: {exc}"
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
            llm=_SkillLLMClient(model=self._resolve_skill_model()),
            scan_result=scan_result,
            skill_logs_dir=user_data.get_agent_skill_logs_dir(self.agent_name),
            skill_eval_dir=user_data.get_agent_skill_eval_dir(self.agent_name),
        )

    def _is_active_hour(self) -> bool:
        """Return True if the current local hour is within active_hours."""
        from datetime import datetime as _dt
        start, end = self.active_hours
        hour = _dt.now().hour
        return start <= hour < end

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

    def _record_skill_log(self, task: Task, output: str, session_id: str) -> None:
        """Record skill invocations to the SLM log after task completion.

        Reads the trajectory file to find which skills the LLM actually
        loaded via _load_resource_skill(). Each unique skill is recorded once.
        """
        if self._skill_log_recorder is None:
            return
        desc = str(getattr(task, "description", "") or "")
        skill_names = self._extract_skills_from_trajectory(session_id)
        for skill_name in skill_names:
            try:
                self._skill_log_recorder.maybe_record(
                    skill_name,
                    session_id=session_id,
                    skill_output=output or "",
                    context_before=desc,
                )
            except Exception as e:
                logger.debug("Failed to record skill log for %s: %s", skill_name, e)

    def _extract_skills_from_trajectory(self, session_id: str) -> list[str]:
        """Extract skill names from trajectory tool_calls of _load_resource_skill."""
        try:
            from ...infra.user_data import get_user_data_manager
            udm = get_user_data_manager()
            traj_path = udm.get_session_trajectory_path(self.agent_name, session_id)
            if not traj_path.exists():
                return []
            data = json.loads(traj_path.read_text(encoding="utf-8"))
            traj = data.get("trajectory", []) if isinstance(data, dict) else []
            seen: set[str] = set()
            for entry in traj:
                if not isinstance(entry, dict) or entry.get("role") != "assistant":
                    continue
                for tc in entry.get("tool_calls", []):
                    fn = tc.get("function", {})
                    if fn.get("name") != "_load_resource_skill":
                        continue
                    args = fn.get("arguments", "")
                    if isinstance(args, str):
                        m = re.search(r'"skill_name"\s*:\s*"([^"]+)"', args)
                        if m:
                            seen.add(m.group(1))
                    elif isinstance(args, dict):
                        name = args.get("skill_name", "")
                        if name:
                            seen.add(name)
            return list(seen)
        except Exception as e:
            logger.debug("Failed to extract skills from trajectory %s: %s", session_id, e)
            return []

    def _write_event(self, event_type: str, **kwargs: Any) -> None:
        """Write a structured event to the JSONL events file."""
        try:
            from ...infra.user_data import get_user_data_manager
            from ...infra.logging_utils import rotate_log_file_if_needed
            user_data = get_user_data_manager()
            events_file = user_data.heartbeat_events_file
            events_file.parent.mkdir(parents=True, exist_ok=True)
            rotate_log_file_if_needed(
                events_file,
                max_bytes=5 * 1024 * 1024,
                backup_count=3,
            )
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
    """Lightweight LLM client for reflection skills.

    Delegates to the shared implementation in heartbeat module.
    """

    def __init__(self, model: str = ""):
        from .heartbeat import _SkillLLMClient as _Impl
        self._impl = _Impl(model=model)

    async def complete(self, prompt: str, system: str = "", model_override: str = "", **kwargs) -> str:
        return await self._impl.complete(prompt, system=system, model_override=model_override, **kwargs)
