"""TaskSession — main multi-phase workflow orchestrator.

Sits between the Scheduling Layer and TurnOrchestrator, enabling
structured research → plan → implement ⟷ verify workflows with
external verification and rollback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from .artifact import build_artifact_injection
from .context_manager import PhaseContextManager
from .exceptions import (
    BudgetExhaustedError,
    CheckpointPauseError,
    PhaseGroupExhaustedError,
)
from .models import (
    PhaseConfig,
    PhaseGroupConfig,
    PhaseResult,
    PhaseTraceEntry,
    TaskSessionConfig,
    TaskSessionEvent,
    TaskSessionState,
    VerifyTraceEntry,
    WorkflowReport,
)
from .phase_runner import PhaseRunner
from .report import generate_report, render_report_markdown
from .verification import extract_verify_result, run_verification_cmd

logger = logging.getLogger(__name__)


class TaskSession:
    """Multi-phase workflow orchestrator.

    Executes phases serially with budget/timeout enforcement,
    PhaseGroup implement⟷verify loops, and rollback on exhaustion.
    """

    def __init__(
        self,
        config: TaskSessionConfig,
        state: TaskSessionState,
        *,
        agent: Any,
        agent_name: str,
        workspace_path: str,
        cancel_event: asyncio.Event,
        skill_dir: str,
        project_dir: str,
        base_instructions: str = "",
    ):
        self.config = config
        self.state = state
        self._agent = agent
        self._agent_name = agent_name
        self._workspace_path = workspace_path
        self._cancel_event = cancel_event
        self._skill_dir = skill_dir
        self._project_dir = project_dir

        self._context_mgr = PhaseContextManager(agent)
        self._phase_runner = PhaseRunner(
            agent=agent,
            context_mgr=self._context_mgr,
            cancel_event=cancel_event,
            skill_dir=skill_dir,
            project_dir=project_dir,
            base_instructions=base_instructions,
        )
        self._phase_traces: List[PhaseTraceEntry] = []

    async def run(self) -> AsyncIterator[TaskSessionEvent]:
        """Execute the full workflow, yielding events."""
        self.state.status = "running"
        self.state.start_time = datetime.now()

        # Capture git HEAD for report
        self.state.git_start_commit = self._get_git_head()

        yield TaskSessionEvent(
            event_type="workflow_start",
            session_id=self.state.session_id,
            data={"workflow_name": self.config.name},
        )

        try:
            while self.state.current_phase_index < len(self.config.phases):
                # Check cancel
                if self._cancel_event.is_set():
                    self.state.status = "cancelled"
                    yield TaskSessionEvent(
                        event_type="workflow_cancelled",
                        session_id=self.state.session_id,
                    )
                    return

                # Check total timeout
                self._check_total_timeout()
                # Check total tool budget
                self._check_total_tool_budget()

                step = self.config.phases[self.state.current_phase_index]

                if isinstance(step, PhaseGroupConfig):
                    try:
                        async for event in self._run_phase_group(step):
                            yield event
                    except PhaseGroupExhaustedError as e:
                        async for event in self._handle_exhausted(step, e):
                            yield event
                        # _handle_exhausted sets current_phase_index to rollback
                        continue
                else:
                    async for event in self._run_single_phase(step):
                        yield event

                self.state.current_phase_index += 1

            self.state.status = "done"

        except BudgetExhaustedError as e:
            self.state.status = "failed"
            logger.error(
                "workflow.budget_exhausted",
                extra={
                    "session_id": self.state.session_id,
                    "budget_type": e.budget_type,
                    "used": e.used,
                    "limit": e.limit,
                },
            )
            yield TaskSessionEvent(
                event_type="workflow_failed",
                session_id=self.state.session_id,
                data={"error": str(e)},
            )
        except CheckpointPauseError as e:
            self.state.status = "paused"
            self._persist_state()
            yield TaskSessionEvent(
                event_type="checkpoint",
                session_id=self.state.session_id,
                phase_name=e.phase_name,
                data={"artifact": e.artifact or ""},
            )
            return

        # Generate report
        report = generate_report(
            state=self.state,
            phase_traces=self._phase_traces,
            config=self.config,
            git_start_commit=self.state.git_start_commit,
            project_dir=self._project_dir,
        )
        report_md = render_report_markdown(report)

        yield TaskSessionEvent(
            event_type="workflow_complete",
            session_id=self.state.session_id,
            data={
                "status": self.state.status,
                "report": report_md,
                "report_json": report.to_dict(),
            },
        )

    async def _run_single_phase(
        self, config: PhaseConfig
    ) -> AsyncIterator[TaskSessionEvent]:
        """Execute a single standalone phase."""
        yield TaskSessionEvent(
            event_type="phase_start",
            session_id=self.state.session_id,
            phase_name=config.name,
        )

        result = await self._phase_runner.run_phase(
            config,
            state=self.state,
            artifacts=self.state.artifacts,
            context_mode="clean",
        )

        # Store artifact
        self.state.artifacts[config.name] = result.artifact

        # Record trace
        self._phase_traces.append(
            PhaseTraceEntry(
                phase_name=config.name,
                phase_type="phase",
                status="completed",
                artifact_preview=result.artifact[:500],
                tool_calls_used=result.tool_calls_used,
                duration_seconds=result.duration_seconds,
            )
        )

        # Checkpoint
        if config.checkpoint:
            self._persist_state()
            raise CheckpointPauseError(
                phase_name=config.name,
                artifact=result.artifact,
            )

        yield TaskSessionEvent(
            event_type="phase_complete",
            session_id=self.state.session_id,
            phase_name=config.name,
            data={"artifact_len": len(result.artifact)},
        )

    async def _run_phase_group(
        self, group: PhaseGroupConfig
    ) -> AsyncIterator[TaskSessionEvent]:
        """Execute a PhaseGroup implement⟷verify loop."""
        # Find action/verify/setup configs by name
        action_config = self._find_group_phase(group, group.action_phase)
        verify_config = self._find_group_phase(group, group.verify_phase)
        setup_config = (
            self._find_group_phase(group, group.setup_phase)
            if group.setup_phase
            else None
        )

        failure_history: List[str] = []
        verify_traces: List[VerifyTraceEntry] = []

        yield TaskSessionEvent(
            event_type="phase_group_start",
            session_id=self.state.session_id,
            phase_name=group.name,
            data={"max_iterations": group.max_iterations},
        )

        # Run setup_phase if present (once per group entry)
        if setup_config:
            yield TaskSessionEvent(
                event_type="phase_start",
                session_id=self.state.session_id,
                phase_name=setup_config.name,
            )
            setup_result = await self._phase_runner.run_phase(
                setup_config,
                state=self.state,
                artifacts=self.state.artifacts,
                context_mode="clean",
            )
            self.state.artifacts[setup_config.name] = setup_result.artifact
            yield TaskSessionEvent(
                event_type="phase_complete",
                session_id=self.state.session_id,
                phase_name=setup_config.name,
            )

        # Iterate action → verify loop
        for iteration in range(1, group.max_iterations + 1):
            self._check_total_timeout()
            self._check_total_tool_budget()

            if self._cancel_event.is_set():
                return

            # Determine context mode
            context_mode = self._context_mgr.determine_context_mode(iteration)

            # Build retry context from last failure
            retry_context = self.state.artifacts.get(
                f"{group.name}__last_failure", ""
            )

            # --- Action phase ---
            yield TaskSessionEvent(
                event_type="phase_start",
                session_id=self.state.session_id,
                phase_name=action_config.name,
                data={"iteration": iteration},
            )

            action_result = await self._phase_runner.run_phase(
                action_config,
                state=self.state,
                artifacts=self.state.artifacts,
                retry_context=retry_context if iteration > 1 else "",
                failure_history=failure_history if iteration > 2 else None,
                context_mode=context_mode,
            )
            self.state.artifacts[action_config.name] = action_result.artifact

            yield TaskSessionEvent(
                event_type="phase_complete",
                session_id=self.state.session_id,
                phase_name=action_config.name,
                data={"iteration": iteration},
            )

            # --- Verify phase ---
            verify_start = time.monotonic()
            passed = False
            verify_output = ""

            if verify_config.verification_cmd:
                # Command-mode verification
                cmd_result = await run_verification_cmd(
                    verify_config.verification_cmd,
                    skill_dir=self._skill_dir,
                    project_dir=self._project_dir,
                    session_id=self.state.session_id,
                )
                passed = cmd_result.exit_code == 0
                verify_output = cmd_result.output
                self.state.artifacts[verify_config.name] = verify_output

                verify_traces.append(
                    VerifyTraceEntry(
                        iteration=iteration,
                        passed=passed,
                        exit_code=cmd_result.exit_code,
                        output=verify_output[:500],
                        duration_seconds=time.monotonic() - verify_start,
                    )
                )
            else:
                # LLM-mode verification
                yield TaskSessionEvent(
                    event_type="phase_start",
                    session_id=self.state.session_id,
                    phase_name=verify_config.name,
                    data={"iteration": iteration},
                )
                verify_result = await self._phase_runner.run_phase(
                    verify_config,
                    state=self.state,
                    artifacts=self.state.artifacts,
                    context_mode="clean",
                    is_verify=True,
                )
                verify_output = verify_result.artifact
                self.state.artifacts[verify_config.name] = verify_output

                passed = extract_verify_result(
                    verify_output, verify_config.verify_protocol
                )

                verify_traces.append(
                    VerifyTraceEntry(
                        iteration=iteration,
                        passed=passed,
                        output=verify_output[:500],
                        duration_seconds=verify_result.duration_seconds,
                    )
                )

                yield TaskSessionEvent(
                    event_type="phase_complete",
                    session_id=self.state.session_id,
                    phase_name=verify_config.name,
                    data={"iteration": iteration},
                )

            if passed:
                logger.info(
                    "workflow.phase_group.verify_passed",
                    extra={
                        "group": group.name,
                        "iteration": iteration,
                    },
                )
                yield TaskSessionEvent(
                    event_type="verify_pass",
                    session_id=self.state.session_id,
                    phase_name=group.name,
                    data={"iteration": iteration},
                )
                # Record phase group trace
                self._phase_traces.append(
                    PhaseTraceEntry(
                        phase_name=group.name,
                        phase_type="phase_group",
                        status="completed",
                        iterations=iteration,
                        verify_traces=verify_traces,
                        tool_calls_used=sum(
                            v.duration_seconds for v in verify_traces
                        ),
                    )
                )
                return

            # Verify failed — record and continue loop
            failure_summary = verify_output[:200] if verify_output else "verification failed"
            failure_history.append(failure_summary)
            self.state.artifacts[f"{group.name}__last_failure"] = verify_output

            logger.info(
                "workflow.phase_group.verify_failed",
                extra={
                    "group": group.name,
                    "iteration": iteration,
                    "failure_preview": failure_summary[:100],
                },
            )
            yield TaskSessionEvent(
                event_type="verify_fail",
                session_id=self.state.session_id,
                phase_name=group.name,
                data={
                    "iteration": iteration,
                    "failure_preview": failure_summary[:100],
                },
            )

        # All iterations exhausted
        self._phase_traces.append(
            PhaseTraceEntry(
                phase_name=group.name,
                phase_type="phase_group",
                status="exhausted",
                iterations=group.max_iterations,
                verify_traces=verify_traces,
                rollback_triggered=group.on_exhausted == "rollback",
            )
        )

        raise PhaseGroupExhaustedError(
            group=group.name,
            iterations=group.max_iterations,
            failure_summary="\n".join(failure_history),
            failure_history=failure_history,
        )

    async def _handle_exhausted(
        self,
        group: PhaseGroupConfig,
        error: PhaseGroupExhaustedError,
    ) -> AsyncIterator[TaskSessionEvent]:
        """Handle PhaseGroup exhaustion with rollback or abort."""
        if group.on_exhausted == "rollback" and group.rollback_target:
            self.state.rollback_retry_count += 1

            if self.state.rollback_retry_count > self.config.max_rollback_retries:
                # Rollback also exhausted — fail workflow
                logger.error(
                    "workflow.rollback_exhausted",
                    extra={
                        "group": group.name,
                        "rollback_count": self.state.rollback_retry_count,
                        "max": self.config.max_rollback_retries,
                    },
                )
                raise BudgetExhaustedError(
                    budget_type="rollback_retries",
                    used=self.state.rollback_retry_count,
                    limit=self.config.max_rollback_retries,
                )

            # Rollback to target phase
            target_index = self._find_phase_index(group.rollback_target)
            self.state.current_phase_index = target_index
            self.state.artifacts["__retry_context"] = error.failure_summary

            logger.info(
                "workflow.rollback",
                extra={
                    "group": group.name,
                    "target": group.rollback_target,
                    "retry_count": self.state.rollback_retry_count,
                },
            )
            yield TaskSessionEvent(
                event_type="rollback",
                session_id=self.state.session_id,
                phase_name=group.name,
                data={
                    "rollback_target": group.rollback_target,
                    "retry_count": self.state.rollback_retry_count,
                },
            )

        elif group.on_exhausted == "checkpoint":
            raise CheckpointPauseError(
                phase_name=group.name,
                artifact=error.failure_summary,
            )
        else:
            # abort
            raise BudgetExhaustedError(
                budget_type="phase_group_exhausted",
                used=error.iterations,
                limit=group.max_iterations,
            )

    # --- Helpers ---

    def _find_group_phase(
        self, group: PhaseGroupConfig, phase_name: str
    ) -> PhaseConfig:
        """Find a phase config by name within a group."""
        for p in group.phases:
            if p.name == phase_name:
                return p
        raise ValueError(
            f"Phase '{phase_name}' not found in group '{group.name}'"
        )

    def _find_phase_index(self, phase_name: str) -> int:
        """Find the index of a top-level phase by name."""
        for i, step in enumerate(self.config.phases):
            name = step.name if isinstance(step, PhaseConfig) else step.name
            if name == phase_name:
                return i
        raise ValueError(f"Phase '{phase_name}' not found in top-level phases")

    def _check_total_timeout(self) -> None:
        """Raise BudgetExhaustedError if total timeout exceeded."""
        if not self.state.start_time:
            return
        elapsed = (datetime.now() - self.state.start_time).total_seconds()
        if elapsed > self.config.total_timeout_seconds:
            raise BudgetExhaustedError(
                budget_type="total_timeout",
                used=int(elapsed),
                limit=self.config.total_timeout_seconds,
            )

    def _check_total_tool_budget(self) -> None:
        """Raise BudgetExhaustedError if total tool calls exceeded."""
        if self.state.total_tool_calls_used > self.config.total_max_tool_calls:
            raise BudgetExhaustedError(
                budget_type="total_tool_calls",
                used=self.state.total_tool_calls_used,
                limit=self.config.total_max_tool_calls,
            )

    def _get_git_head(self) -> Optional[str]:
        """Get current git HEAD commit hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self._project_dir,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _persist_state(self) -> None:
        """Persist state to JSON for checkpoint."""
        try:
            state_dir = os.path.expanduser(
                f"~/.alfred/sessions/{self.state.session_id}"
            )
            os.makedirs(state_dir, exist_ok=True)
            state_path = os.path.join(state_dir, "state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(self.state.to_dict(), f, indent=2, ensure_ascii=False)
            logger.info(
                "workflow.state.persisted",
                extra={"path": state_path},
            )
        except Exception as e:
            logger.error(
                "workflow.state.persist_failed",
                extra={"error": str(e)},
            )
