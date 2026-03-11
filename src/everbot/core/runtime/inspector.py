"""Inspector — heartbeat reflection observation engine.

Observes session context, MEMORY.md, task execution stats, and recent events to discover
and register new routine tasks. Does NOT execute any tasks — that is the Cron side
of the heartbeat.

Wraps ReflectionManager internally and exposes a clean ``inspect()`` method
that the Scheduler / HeartbeatRunner can call on a reflection tick.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...infra.user_data import get_user_data_manager
from ..models.system_event import build_system_event
from .reflection import ReflectionManager

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 500

# State file for tracking inspection context changes
INSPECTOR_STATE_FILE = ".inspector_state.json"


# ── Result types ─────────────────────────────────────────────


@dataclass
class InspectionContext:
    """Context provided to the reflection LLM call."""

    memory_content: Optional[str] = None
    heartbeat_content: Optional[str] = None
    session_summary: Optional[str] = None
    task_execution_stats: Dict[str, Any] = field(default_factory=dict)
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    existing_routines: List[Any] = field(default_factory=list)
    idle_hours: Optional[float] = None
    system_health: Optional[str] = None


@dataclass
class InspectionResult:
    """Outcome of one inspection cycle."""

    heartbeat_ok: bool = True
    push_message: Optional[str] = None
    delivery_detail: Optional[str] = None
    proposals: List[dict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None
    applied: int = 0
    deposited: int = 0
    output: str = "HEARTBEAT_OK"


async def emit_push_message(
    push_message: str,
    *,
    primary_session_id: str,
    agent_name: str,
    run_id: str,
    scope: str = "agent",
    detail: Optional[str] = None,
) -> None:
    """Emit an inspector push_message as a heartbeat_delivery event.

    Shared by HeartbeatRunner and daemon's _run_inspector to avoid duplicated
    event-building logic.

    Args:
        push_message: Concise user-facing summary (sent to Telegram).
        detail: Full job result for LLM context. Falls back to push_message
                if not provided (backward compatibility).
    """
    from .events import emit
    routing_kwargs = {
        "scope": scope,
        "target_session_id": primary_session_id if scope == "session" else None,
    }

    await emit(
        primary_session_id,
        {
            "type": "message",
            "role": "assistant",
            "content": push_message,
            "summary": push_message[:SUMMARY_MAX_CHARS],
            "detail": detail or push_message,
            "source_type": "inspector_push",
            "run_id": run_id,
            "deliver": True,
        },
        agent_name=agent_name,
        **routing_kwargs,
        source_type="inspector_push",
        run_id=run_id,
    )


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
        self._force_interval = timedelta(hours=reflect_force_interval_hours)

        self._reflection = reflection_manager or ReflectionManager(
            workspace_path=self.workspace_path,
            force_interval=self._force_interval,
        )

        self._state_path = self._resolve_state_path()

        # Restore ReflectionManager in-memory state from persisted state
        # so that process restarts don't force an unnecessary LLM call.
        persisted = self._load_state()
        if persisted.get("last_run_at"):
            try:
                self._reflection.last_reflect_at = datetime.fromisoformat(
                    persisted["last_run_at"]
                )
                self._reflection.last_reflect_file_hashes = (
                    self._reflection.compute_file_hashes()
                )
            except (TypeError, ValueError):
                pass

    # ── State Management ───────────────────────────────────────

    def _resolve_state_path(self) -> Path:
        """Resolve persisted inspector state path outside the workspace root."""
        try:
            return (
                get_user_data_manager().get_agent_tmp_dir(self.agent_name)
                / INSPECTOR_STATE_FILE
            )
        except Exception as exc:
            logger.debug("Failed to resolve inspector state path from user data: %s", exc)
            return self.workspace_path / INSPECTOR_STATE_FILE

    def _load_state(self) -> Dict[str, Any]:
        """Load persisted inspector state."""
        try:
            if self._state_path.exists():
                return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to load inspector state: %s", exc)
        return {}

    def _persist_state(self, state: Dict[str, Any]) -> None:
        """Persist inspector state."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Failed to persist inspector state: %s", exc)

    def _compute_context_hashes(self, ctx: InspectionContext) -> Dict[str, str]:
        """Compute hashes for all context components to detect changes."""
        hashes: Dict[str, str] = {
            "session_summary": hashlib.sha256(
                str(ctx.session_summary or "").encode("utf-8")
            ).hexdigest(),
            "task_stats": hashlib.sha256(
                json.dumps(ctx.task_execution_stats, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "events": hashlib.sha256(
                json.dumps(ctx.recent_events, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "memory": hashlib.sha256(str(ctx.memory_content or "").encode("utf-8")).hexdigest(),
            "heartbeat": hashlib.sha256(
                str(ctx.heartbeat_content or "").encode("utf-8")
            ).hexdigest(),
        }
        return hashes

    # ── Context Gathering ──────────────────────────────────────

    def _gather_context(
        self,
        heartbeat_content: str,
        session_manager: Optional[Any] = None,
        primary_session_id: Optional[str] = None,
    ) -> InspectionContext:
        """Gather enriched context for reflection prompt."""
        memory_path = self.workspace_path / "MEMORY.md"
        memory_content = None
        if memory_path.exists():
            try:
                memory_content = memory_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("Failed to read MEMORY.md: %s", exc)

        # Get session summary from primary session
        session_summary = None
        if session_manager and primary_session_id:
            try:
                summary = session_manager.get_session_summary(
                    primary_session_id, max_chars=SUMMARY_MAX_CHARS
                )
                if isinstance(summary, str) and summary.strip():
                    session_summary = summary
            except Exception as exc:
                logger.debug("Failed to get session summary: %s", exc)

        # Get task execution stats
        task_stats = self._gather_task_stats()

        # Get recent events (last 24h)
        recent_events = self._gather_recent_events()

        # Get existing routines
        existing_routines = []
        if self.routine_manager:
            try:
                existing_routines = list(self.routine_manager.list_routines())
            except Exception as exc:
                logger.debug("Failed to list routines: %s", exc)

        # System health snapshot (process resources + disk)
        system_health = None
        try:
            from ..jobs.health_check import _check_process_resources, _check_session_storage
            proc = _check_process_resources()
            storage = _check_session_storage(
                type("_Ctx", (), {"sessions_dir": self.workspace_path / "sessions"})()
            )
            parts = [f"进程: {proc.message}"]
            parts.append(f"存储: {storage.message}")
            system_health = "; ".join(parts)
        except Exception as exc:
            logger.debug("Failed to gather system health: %s", exc)

        # Compute idle hours since last user interaction
        idle_hours = None
        if session_manager:
            try:
                last_activity = session_manager.get_last_activity_time(self.agent_name)
                if last_activity is not None and isinstance(last_activity, (int, float)):
                    idle_hours = round((time.time() - last_activity) / 3600, 1)
            except Exception as exc:
                logger.debug("Failed to get last activity time: %s", exc)

        return InspectionContext(
            memory_content=memory_content,
            heartbeat_content=heartbeat_content,
            session_summary=session_summary,
            task_execution_stats=task_stats,
            recent_events=recent_events,
            existing_routines=existing_routines,
            idle_hours=idle_hours,
            system_health=system_health,
        )

    def _gather_task_stats(self) -> Dict[str, Any]:
        """Gather task execution statistics."""
        stats = {"total": 0, "failed": 0, "pending": 0, "last_24h": 0}
        try:
            if self.routine_manager and hasattr(self.routine_manager, "list_routines"):
                tasks = list(self.routine_manager.list_routines())
                stats["total"] = len(tasks)
                now = datetime.now()
                for task in tasks:
                    task_state = None
                    if isinstance(task, dict):
                        task_state = task.get("state") or task.get("status")
                        last_run_at = task.get("last_run_at")
                    else:
                        task_state = getattr(task, "state", None) or getattr(
                            task, "status", None
                        )
                        last_run_at = getattr(task, "last_run_at", None)

                    if task_state == "failed":
                        stats["failed"] += 1
                    elif task_state == "pending":
                        stats["pending"] += 1

                    if not last_run_at:
                        continue
                    try:
                        last_run = datetime.fromisoformat(str(last_run_at))
                    except (TypeError, ValueError):
                        continue
                    if last_run.tzinfo is not None:
                        now_for_compare = datetime.now(last_run.tzinfo)
                    else:
                        now_for_compare = now
                    if (now_for_compare - last_run).total_seconds() < 86400:
                        stats["last_24h"] += 1
        except Exception as exc:
            logger.debug("Failed to gather task stats: %s", exc)
        return stats

    def _gather_recent_events(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Gather recent system events."""
        events = []
        try:
            user_data = get_user_data_manager()
            events_file = user_data.heartbeat_events_file
            if events_file.exists():
                cutoff = datetime.now() - timedelta(hours=hours)
                with open(events_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            event = json.loads(line.strip())
                            ts = event.get("timestamp")
                            if ts:
                                event_time = datetime.fromisoformat(ts)
                                if event_time >= cutoff:
                                    events.append(event)
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug("Failed to gather recent events: %s", exc)
        return events[-10:]  # Limit to 10 most recent

    # ── Reflection entrypoint ──────────────────────────────────

    # ── Skip / state logic ─────────────────────────────────────

    def _should_inspect(self, ctx: InspectionContext) -> bool:
        """Return True if inspection should proceed based on context changes.

        Checks ReflectionManager's file-based logic first, then persisted
        context hashes for enriched change detection.
        """
        state = self._load_state()

        # Always inspect when idle_hours has grown significantly since last run,
        # so the LLM gets a chance to decide whether to proactively reach out.
        last_idle = state.get("last_idle_hours")
        if ctx.idle_hours is not None and ctx.idle_hours >= 1.0:
            if last_idle is None or ctx.idle_hours - last_idle >= 1.0:
                return True

        # Check ReflectionManager's built-in file-change / force-interval logic
        rm_decision = None
        if hasattr(self._reflection, 'should_skip_reflection'):
            try:
                rm_skip = self._reflection.should_skip_reflection()
                if not rm_skip:
                    return True  # files changed or force interval elapsed
                rm_decision = "skip"  # RM says skip (files unchanged)
            except Exception:
                pass

        last_hashes = state.get("context_hashes", {})
        last_run_at = state.get("last_run_at")

        # Check force interval from persisted state
        if self._force_interval and last_run_at:
            last_time = datetime.fromisoformat(last_run_at)
            if datetime.now() - last_time >= self._force_interval:
                return True

        # If RM already checked and no persisted state exists, trust RM
        if not last_hashes:
            if rm_decision == "skip":
                return False  # RM says files unchanged, no prior enriched state
            return True  # first time ever

        # Compare context hashes
        current_hashes = self._compute_context_hashes(ctx)
        for key, current_hash in current_hashes.items():
            if last_hashes.get(key) != current_hash:
                return True

        return False

    def should_skip(
        self,
        *,
        session_manager: Optional[Any] = None,
        primary_session_id: Optional[str] = None,
    ) -> bool:
        """Public API: check if inspection can be skipped (backward compat).

        Gathers context internally and delegates to ``_should_inspect``.
        """
        heartbeat_path = self.workspace_path / "HEARTBEAT.md"
        heartbeat_content = ""
        if heartbeat_path.exists():
            try:
                heartbeat_content = heartbeat_path.read_text(encoding="utf-8")
            except Exception:
                pass

        ctx = self._gather_context(
            heartbeat_content,
            session_manager=session_manager,
            primary_session_id=primary_session_id,
        )
        return not self._should_inspect(ctx)

    def update_state(
        self,
        ctx: Optional[InspectionContext] = None,
        result: Optional[InspectionResult] = None,
    ) -> None:
        """Persist context hashes after a successful inspection.

        Args:
            ctx: pre-gathered context (gathered internally if omitted).
            result: inspection result (unused, kept for API symmetry).
        """
        # Update ReflectionManager's state for backward compatibility
        if hasattr(self._reflection, 'update_reflect_state'):
            try:
                self._reflection.update_reflect_state()
            except Exception:
                pass

        if ctx is None:
            heartbeat_path = self.workspace_path / "HEARTBEAT.md"
            heartbeat_content = ""
            if heartbeat_path.exists():
                try:
                    heartbeat_content = heartbeat_path.read_text(encoding="utf-8")
                except Exception:
                    pass
            ctx = self._gather_context(heartbeat_content)

        state = {
            "last_run_at": datetime.now().isoformat(),
            "context_hashes": self._compute_context_hashes(ctx),
            "last_idle_hours": ctx.idle_hours,
        }
        self._persist_state(state)

    async def inspect(
        self,
        *,
        run_agent: Callable[..., Any],
        inject_context: Callable[..., Any],
        agent: Any,
        heartbeat_content: str,
        run_id: str,
        session_manager: Optional[Any] = None,
        primary_session_id: Optional[str] = None,
    ) -> InspectionResult:
        """Run one inspection cycle.

        If ``auto_register_routines`` is True, new routines are auto-registered.
        Otherwise, proposals are deposited to the primary session mailbox.
        """
        # Gather enriched context once
        ctx = self._gather_context(heartbeat_content, session_manager, primary_session_id)

        # Check if we should skip
        if not self._should_inspect(ctx):
            self._write_event("inspect_skipped", reason="no_context_change")
            return InspectionResult(
                skipped=True,
                skip_reason="no_context_change",
                output="HEARTBEAT_OK",
            )

        # Build enriched prompt and inject into agent
        reflect_prompt = self._build_reflect_prompt(ctx)
        user_message = await inject_context(agent, reflect_prompt, mode="reflect_json")

        # Run LLM
        try:
            response = await run_agent(agent, user_message)
        except Exception as exc:
            logger.warning("LLM reflection failed: %s", exc)
            return InspectionResult(
                heartbeat_ok=False,
                output=f"LLM_ERROR: {exc}",
            )

        # Parse response (unified format)
        parsed = self._reflection.extract_unified_response(response)

        # Update state after successful LLM call
        self.update_state(ctx)

        result = InspectionResult(
            heartbeat_ok=parsed.heartbeat_ok,
            push_message=parsed.push_message,
            output="HEARTBEAT_OK" if parsed.heartbeat_ok else "HEARTBEAT_ERROR",
        )

        # Handle routine proposals
        if parsed.routines:
            result.proposals = parsed.routines
            result.applied, result.deposited = await self._apply_proposals(
                proposals=parsed.routines,
                session_manager=session_manager,
                primary_session_id=primary_session_id,
                run_id=run_id,
            )

        # Deliver push message if present
        if result.push_message and session_manager and primary_session_id:
            delivered = await self._deliver_push_message(
                result=result,
                session_manager=session_manager,
                primary_session_id=primary_session_id,
                run_id=run_id,
            )
            if delivered:
                result.deposited += 1

        self._write_event(
            "inspection_complete",
            heartbeat_ok=result.heartbeat_ok,
            push_message=bool(result.push_message),
            proposals_count=len(result.proposals),
            applied=result.applied,
            deposited=result.deposited,
            run_id=run_id,
        )

        return result

    def _build_reflect_prompt(self, ctx: InspectionContext) -> str:
        """Build reflection prompt with enriched context."""
        sections = []

        # Idle duration section (placed first for prominence)
        if ctx.idle_hours is not None:
            sections.append(f"# 用户活跃状态\n距离用户上次互动已过去 {ctx.idle_hours} 小时。")

        # Memory section
        if ctx.memory_content:
            sections.append(f"# MEMORY.md\n{ctx.memory_content[:2000]}")

        # Heartbeat section
        if ctx.heartbeat_content:
            sections.append(f"# HEARTBEAT.md\n{ctx.heartbeat_content[:2000]}")

        # Session summary section
        if ctx.session_summary:
            sections.append(f"# Recent Session Summary\n{ctx.session_summary}")

        # Task stats section
        if ctx.task_execution_stats:
            stats_text = json.dumps(ctx.task_execution_stats, indent=2)
            sections.append(f"# Task Execution Stats\n{stats_text}")

        # Recent events section
        if ctx.recent_events:
            events_text = json.dumps(ctx.recent_events[:5], indent=2)
            sections.append(f"# Recent Events (last 24h)\n{events_text}")

        # System health section
        if ctx.system_health:
            sections.append(f"# System Health\n{ctx.system_health}")

        # Existing routines section
        if ctx.existing_routines:
            routines_text = "\n".join(
                f"- {r.title if hasattr(r, 'title') else r.get('title', 'unknown')}"
                for r in ctx.existing_routines[:10]
            )
            sections.append(f"# Existing Routines\n{routines_text}")

        # Instructions
        sections.append(
            """# Reflection Instructions

你是用户的私人助理。根据上述上下文，做出以下判断：

1. **系统健康**：上述上下文是否显示系统存在需要用户关注的问题？(heartbeat_ok)
2. **主动沟通**：综合考虑用户的兴趣、习惯、当前任务状态和空闲时长，判断现在是否值得主动给用户发一条消息。可以是任务执行情况的总结、基于用户兴趣的洞察、对近期工作的回顾、或任何你认为用户会感兴趣的话题。没什么值得说的就保持沉默。(push_message)
3. **Routine 提议**：是否需要提议新的定时任务？(routines)

以如下 JSON 格式回复：
```json
{
  "heartbeat_ok": true,
  "push_message": null,
  "routines": []
}
```

- heartbeat_ok: false 表示系统存在需要关注的问题
- push_message: 你想主动发给用户的消息内容（会立即推送），null 表示保持沉默
- routines: routine 任务提议数组

## push_message 语气要求
push_message 是直接发送给用户的消息。请用**自然的助手口吻**书写，就像你在和用户聊天一样。
- 用关心的语气，比如："我注意到最近 kimi 的接口不太稳定，连着失败了好几次，我先用备选模型顶上了，你看需要我做什么调整吗？"
- 如果一切正常想主动聊聊，就像朋友随口说一句，比如："今天跑了几个任务都挺顺利的，没什么需要操心的。"
- 用大白话，不要写成系统报告的格式"""
        )

        return "\n\n".join(sections)

    async def _apply_proposals(
        self,
        proposals: List[dict],
        session_manager: Optional[Any],
        primary_session_id: Optional[str],
        run_id: str,
    ) -> Tuple[int, int]:
        """Apply routine proposals via auto-register or deposit to mailbox."""
        applied = 0
        deposited = 0

        for raw in proposals:
            normalized = self._reflection.normalize_routine(raw)
            if normalized is None:
                continue

            if self.auto_register_routines:
                try:
                    self.routine_manager.add_routine(**normalized)
                    applied += 1
                    self._write_event(
                        "routine_auto_registered",
                        title=normalized.get("title"),
                        run_id=run_id,
                    )
                except ValueError as exc:
                    detail = str(exc)
                    if "duplicate routine" in detail or "task_id already exists" in detail:
                        logger.debug("Skipping duplicate routine: %s", normalized.get("title"))
                    else:
                        logger.warning("Failed to auto-register routine: %s", exc)
                except Exception as exc:
                    logger.warning("Failed to auto-register routine: %s", exc)
            else:
                # Deposit to mailbox for user review
                if session_manager and primary_session_id:
                    try:
                        event = build_system_event(
                            event_type="routine_proposal",
                            source_session_id=f"inspector:{self.agent_name}",
                            summary=f"Routine proposal: {normalized.get('title', '')}",
                            detail=normalized.get("description", ""),
                            artifacts=[normalized],
                            priority=1,
                            suppress_if_stale=False,
                            dedupe_key=f"routine_proposal:{self.agent_name}:{normalized.get('title')}:{run_id}",
                        )
                        await session_manager.deposit_mailbox_event(
                            primary_session_id,
                            event,
                            timeout=5.0,
                            blocking=True,
                        )
                        deposited += 1
                        self._write_event(
                            "routine_proposal_deposited",
                            title=normalized.get("title"),
                            run_id=run_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to deposit proposal: %s", exc)

        return applied, deposited

    async def _deliver_push_message(
        self,
        result: InspectionResult,
        session_manager: Optional[Any],
        primary_session_id: Optional[str],
        run_id: str,
    ) -> bool:
        """Deliver urgent push message to primary session."""
        if not session_manager or not primary_session_id or not result.push_message:
            return False

        try:
            event = build_system_event(
                event_type="inspector_push",
                source_session_id=f"inspector:{self.agent_name}",
                summary=result.push_message[:SUMMARY_MAX_CHARS],
                detail=result.delivery_detail or result.push_message,
                artifacts=[],
                priority=1,
                suppress_if_stale=False,
                dedupe_key=f"inspector_push:{self.agent_name}:{run_id}",
            )
            await session_manager.deposit_mailbox_event(
                primary_session_id,
                event,
                timeout=5.0,
                blocking=True,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to deliver push message: %s", exc)
            return False

    def _write_event(self, event_type: str, **kwargs: Any) -> None:
        """Write a structured event to heartbeat_events.jsonl."""
        try:
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
