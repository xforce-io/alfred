"""Single-phase execution via TurnOrchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..runtime.turn_orchestrator import (
    TurnEventType,
    TurnOrchestrator,
    TurnPolicy,
    WORKFLOW_POLICY,
)
from .artifact import build_artifact_injection, extract_artifact
from .context_manager import PhaseContextManager
from .models import PhaseConfig, PhaseResult, TaskSessionState

logger = logging.getLogger(__name__)


class PhaseRunner:
    """Executes a single phase using TurnOrchestrator."""

    def __init__(
        self,
        agent: Any,
        context_mgr: PhaseContextManager,
        cancel_event: asyncio.Event,
        skill_dir: str,
        project_dir: str,
        base_instructions: str = "",
    ):
        self._agent = agent
        self._context_mgr = context_mgr
        self._cancel_event = cancel_event
        self._skill_dir = skill_dir
        self._project_dir = project_dir
        self._base_instructions = base_instructions

    async def run_phase(
        self,
        phase_config: PhaseConfig,
        *,
        state: TaskSessionState,
        artifacts: Dict[str, str],
        retry_context: str = "",
        failure_history: Optional[List[str]] = None,
        context_mode: str = "clean",
        is_verify: bool = False,
    ) -> PhaseResult:
        """Execute a single LLM-driven phase.

        1. Load instruction from instruction_ref
        2. Build system prompt
        3. Build artifact injection
        4. Prepare context (clear/inherit history, build user message)
        5. Create TurnPolicy from phase_config
        6. Loop up to max_turns via TurnOrchestrator
        7. Extract artifact
        8. Return PhaseResult
        """
        start_time = time.monotonic()

        # Load instruction content
        instruction_content = ""
        if phase_config.instruction_ref:
            instruction_path = os.path.join(
                self._skill_dir, phase_config.instruction_ref
            )
            if os.path.isfile(instruction_path):
                with open(instruction_path, "r", encoding="utf-8") as f:
                    instruction_content = f.read()
            else:
                logger.warning(
                    "workflow.phase.instruction_not_found",
                    extra={
                        "phase": phase_config.name,
                        "path": instruction_path,
                    },
                )

        # Build system prompt
        system_prompt = self._context_mgr.build_phase_system_prompt(
            self._base_instructions,
            phase_config,
            instruction_content=instruction_content,
            is_verify=is_verify,
        )

        # Build artifact injection
        artifact_injection = build_artifact_injection(
            artifacts, phase_config.input_artifacts
        )

        # Prepare user message (handles history clearing)
        user_message = self._context_mgr.prepare_phase_context(
            artifact_injection=artifact_injection,
            retry_context=retry_context,
            failure_history=failure_history or [],
            context_mode=context_mode,
        )

        # Build TurnPolicy from phase config
        policy = TurnPolicy(
            max_attempts=WORKFLOW_POLICY.max_attempts,
            max_tool_calls=phase_config.max_tool_calls,
            max_failed_tool_outputs=WORKFLOW_POLICY.max_failed_tool_outputs,
            max_tool_output_preview_chars=WORKFLOW_POLICY.max_tool_output_preview_chars,
            timeout_seconds=phase_config.timeout_seconds,
        )

        orchestrator = TurnOrchestrator(policy=policy)

        # Execute turns
        total_tool_calls = 0
        full_response = ""
        last_assistant_text = ""

        for turn_num in range(1, phase_config.max_turns + 1):
            if self._cancel_event.is_set():
                logger.info(
                    "workflow.phase.cancelled",
                    extra={"phase": phase_config.name, "turn": turn_num},
                )
                break

            logger.info(
                "workflow.phase.turn_start",
                extra={
                    "phase": phase_config.name,
                    "turn": turn_num,
                    "max_turns": phase_config.max_turns,
                },
            )

            turn_response = ""
            turn_tool_calls = 0

            async for event in orchestrator.run_turn(
                self._agent,
                user_message if turn_num == 1 else "继续完成本阶段的工作。",
                system_prompt=system_prompt,
                cancel_event=self._cancel_event,
            ):
                if event.type == TurnEventType.LLM_DELTA:
                    turn_response += event.content
                elif event.type == TurnEventType.TURN_COMPLETE:
                    turn_response = event.answer or turn_response
                    turn_tool_calls = event.tool_call_count
                elif event.type == TurnEventType.TURN_ERROR:
                    logger.warning(
                        "workflow.phase.turn_error",
                        extra={
                            "phase": phase_config.name,
                            "turn": turn_num,
                            "error": event.error,
                        },
                    )
                    turn_tool_calls = event.tool_call_count
                    break

            full_response += turn_response
            total_tool_calls += turn_tool_calls
            state.total_tool_calls_used += turn_tool_calls

            if turn_response:
                last_assistant_text = turn_response

            # Check for <phase_artifact> tag → LLM signaled completion
            if "<phase_artifact>" in turn_response:
                logger.info(
                    "workflow.phase.artifact_found",
                    extra={"phase": phase_config.name, "turn": turn_num},
                )
                break

        # Extract artifact
        artifact = extract_artifact(
            phase_config.name, full_response, last_assistant_text
        )

        duration = time.monotonic() - start_time
        logger.info(
            "workflow.phase.complete",
            extra={
                "phase": phase_config.name,
                "tool_calls": total_tool_calls,
                "duration_seconds": round(duration, 1),
                "artifact_len": len(artifact),
            },
        )

        return PhaseResult(
            artifact=artifact,
            tool_calls_used=total_tool_calls,
            full_response=full_response,
            duration_seconds=duration,
        )
