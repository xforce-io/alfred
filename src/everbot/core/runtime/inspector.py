"""Inspector — heartbeat reflection observation engine.

Observes session context, MEMORY.md, and task execution stats to discover
and register new routine tasks. Does NOT execute any tasks — that is the
Cron side of the heartbeat.

Wraps ReflectionManager internally and exposes a clean ``inspect()`` method
that the Scheduler / HeartbeatRunner can call on a reflection tick.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..models.system_event import build_system_event
from .reflection import ReflectionManager

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 500


# ── Result types ─────────────────────────────────────────────


@dataclass
class InspectionContext:
    """Context provided to the reflection LLM call."""

    memory_content: Optional[str] = None
    session_summary: Optional[str] = None
    task_execution_stats: Dict[str, Any] = field(default_factory=dict)
    existing_routines: List[Any] = field(default_factory=list)


@dataclass
class InspectionResult:
    """Outcome of one inspection cycle."""

    proposals: List[dict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None
    applied: int = 0
    deposited: int = 0
    output: str = "HEARTBEAT_OK"


# ── Inspector ────────────────────────────────────────────────


class Inspector:
    """Heartbeat reflection observation engine.

    Discovers routine task proposals by running the LLM agent in reflection
    mode, then either auto-registers them via RoutineManager or deposits
    them to the primary session mailbox for user review.

    Like CronExecutor, Inspector takes callables (``run_agent``,
    ``inject_context``) to avoid tight coupling to the agent runtime.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        workspace_path: Path,
        routine_manager: Any,
        reflection_manager: Optional[ReflectionManager] = None,
        auto_register_routines: bool = False,
        reflect_force_interval_hours: float = 24,
    ):
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.routine_manager = routine_manager
        self.auto_register_routines = auto_register_routines

        self._reflection = reflection_manager or ReflectionManager(
            workspace_path=self.workspace_path,
            force_interval=timedelta(hours=reflect_force_interval_hours),
        )

    # ── Public API ────────────────────────────────────────────

    def should_skip(self) -> bool:
        """Check whether inspection can be skipped (files unchanged + force interval not elapsed)."""
        return self._reflection.should_skip_reflection()

    async def inspect(
        self,
        *,
        run_agent: Callable,
        inject_context: Callable,
        agent: Any,
        heartbeat_content: str,
        run_id: str,
        session_manager: Any = None,
        primary_session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> InspectionResult:
        """Execute one inspection cycle.

        Args:
            run_agent: async callable ``(agent, user_message) -> str``
            inject_context: async callable ``(agent, content, mode="reflect") -> str``
            agent: the LLM agent instance
            heartbeat_content: raw HEARTBEAT.md content
            run_id: current heartbeat run id
            session_manager: optional, needed for mailbox deposit when not auto-registering
            primary_session_id: optional, needed for mailbox deposit
            agent_name: override agent_name for logging (defaults to self.agent_name)
        """
        effective_agent_name = agent_name or self.agent_name

        # 1. Skip check
        if self.should_skip():
            logger.info(
                "[%s] Inspection skipped: files unchanged since last reflect",
                effective_agent_name,
            )
            self._write_event("inspect_skipped", reason="file_unchanged")
            return InspectionResult(
                skipped=True,
                skip_reason="file_unchanged",
            )

        # 2. Inject reflection context and run LLM
        user_message = await inject_context(agent, heartbeat_content, mode="reflect")
        response = await run_agent(agent, user_message)

        # 3. Record state after successful LLM call
        self.update_state()

        # 4. Extract proposals
        proposals = self._reflection.extract_routine_proposals(response)

        if not proposals:
            logger.debug(
                "[%s] Inspection produced no proposals, returning HEARTBEAT_OK",
                effective_agent_name,
            )
            self._write_event("inspect", result="ok")
            return InspectionResult(output="HEARTBEAT_OK")

        # 5. Apply or deposit proposals
        self._write_event("inspect", result="proposals", proposal_count=len(proposals))

        if self.auto_register_routines:
            return self._apply_proposals(proposals, run_id, effective_agent_name)
        else:
            return await self._deposit_proposals(
                proposals,
                run_id,
                effective_agent_name,
                session_manager=session_manager,
                primary_session_id=primary_session_id,
            )

    def update_state(self) -> None:
        """Record state after a successful inspection (delegates to ReflectionManager)."""
        self._reflection.update_reflect_state()

    # ── Internal ──────────────────────────────────────────────

    def _apply_proposals(
        self,
        proposals: List[dict],
        run_id: str,
        agent_name: str,
    ) -> InspectionResult:
        """Auto-register proposals via RoutineManager."""
        added: List[dict] = []
        skipped_duplicates = 0
        failed = 0

        for raw in proposals:
            normalized = self._reflection.normalize_routine(raw)
            if normalized is None:
                continue
            try:
                created = self.routine_manager.add_routine(**normalized)
                added.append(created)
            except ValueError as exc:
                detail = str(exc)
                if "duplicate routine" in detail or "task_id already exists" in detail:
                    skipped_duplicates += 1
                else:
                    failed += 1
                    logger.warning(
                        "Inspector routine apply rejected: agent=%s run_id=%s title=%s reason=%s",
                        agent_name, run_id,
                        normalized.get("title", ""), detail,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Inspector routine apply failed: agent=%s run_id=%s title=%s error=%s",
                    agent_name, run_id,
                    normalized.get("title", ""), str(exc),
                )

        lines = [f"Registered {len(added)} routine(s) from heartbeat inspection."]
        for item in added[:5]:
            title = str(item.get("title") or "")
            schedule = str(item.get("schedule") or "manual")
            lines.append(f"- {title} ({schedule})")
        if skipped_duplicates > 0:
            lines.append(f"Skipped duplicates: {skipped_duplicates}.")
        if failed > 0:
            lines.append(f"Failed to apply: {failed}.")

        output = "\n".join(lines) if added else "HEARTBEAT_OK"

        return InspectionResult(
            proposals=proposals,
            applied=len(added),
            output=output,
        )

    async def _deposit_proposals(
        self,
        proposals: List[dict],
        run_id: str,
        agent_name: str,
        *,
        session_manager: Any = None,
        primary_session_id: Optional[str] = None,
    ) -> InspectionResult:
        """Deposit proposals to the primary session mailbox for user review."""
        lines = [f"Heartbeat inspection proposed {len(proposals)} routine(s) for review:"]
        normalized_list: List[dict] = []
        for raw in proposals:
            normalized = self._reflection.normalize_routine(raw)
            if normalized is None:
                continue
            normalized_list.append(normalized)
            title = normalized.get("title", "")
            schedule = normalized.get("schedule") or "manual"
            lines.append(f"- {title} ({schedule})")

        summary = "\n".join(lines)

        if session_manager is not None and primary_session_id is not None:
            event = build_system_event(
                event_type="routine_proposal",
                source_session_id=f"inspector:{agent_name}",
                summary=summary[:SUMMARY_MAX_CHARS],
                detail=json.dumps(normalized_list, ensure_ascii=False, indent=2),
                artifacts=[],
                priority=0,
                suppress_if_stale=False,
                dedupe_key=f"routine_proposal:{agent_name}:{run_id}",
            )
            await session_manager.deposit_mailbox_event(
                primary_session_id, event, timeout=5.0, blocking=True,
            )
        else:
            logger.warning(
                "[%s] Cannot deposit proposals: session_manager or primary_session_id not provided",
                agent_name,
            )

        return InspectionResult(
            proposals=proposals,
            deposited=len(normalized_list),
            output=summary,
        )

    def _write_event(self, event_type: str, **kwargs: Any) -> None:
        """Write a structured event to heartbeat_events.jsonl."""
        try:
            from ...infra.user_data import get_user_data_manager

            user_data = get_user_data_manager()
            events_file = user_data.heartbeat_events_file
            events_file.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "timestamp": datetime.now().isoformat(),
                "agent": self.agent_name,
                "source": "inspector",
                "event": event_type,
            }
            event.update(kwargs)
            with open(events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("Failed to write inspector event: %s", exc)
