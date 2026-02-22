"""
心跳运行器
"""

import asyncio
import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, Any, Dict
import uuid
import logging
from types import SimpleNamespace

from ...infra.dolphin_compat import ensure_continue_chat_compatibility
from ..models.system_event import build_system_event
from . import RuntimeDeps, TurnExecutor
from ..tasks.routine_manager import RoutineManager
from ..session.session import SessionPersistence
from ..tasks.task_manager import (
    Task,
    parse_heartbeat_md,
    get_due_tasks,
    claim_task,
    update_task_state,
    write_task_block,
    purge_stale_tasks,
    TaskList,
    TaskState,
    ParseResult,
    ParseStatus,
)
from ...infra.user_data import UserDataManager

logger = logging.getLogger(__name__)

# Error markers that indicate non-retryable (permanent) failures.
# Matching is case-insensitive against ``str(exception)``.
_PERMANENT_ERROR_MARKERS: list[str] = [
    "402",
    "403",
    "401",
    "insufficient balance",
    "insufficient_quota",
    "quota exceeded",
    "rate_limit",           # distinct from transient 429 retry-after
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "authorization",
    "missing env var",
    "billing",
    "account deactivated",
    "access denied",
]


def _is_permanent_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a non-retryable error."""
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_ERROR_MARKERS)


class HeartbeatRunner:
    """
    心跳运行器

    定期唤醒 Agent 检查任务清单并执行。
    """

    SUMMARY_MAX_CHARS: int = 500

    _HEARTBEAT_INST_START = "<!-- EVERBOT_HEARTBEAT_INSTRUCTION:START -->"
    _HEARTBEAT_INST_END = "<!-- EVERBOT_HEARTBEAT_INSTRUCTION:END -->"
    HEARTBEAT_SYSTEM_INSTRUCTION = """
## Heartbeat Mode

You are running periodic heartbeat checks.

### Phase 1: Execute due tasks
Prioritize tasks where `state=pending` and `next_run_at <= now`.
Execute due tasks and summarize the outcome.

### Phase 2: Routine reflection (only if no due tasks)
Review MEMORY.md (if it exists) and recent context for recurring intentions that are not scheduled yet.
If you find one, propose it in a JSON block:
```json
{"routines":[{"title":"...", "description":"...", "schedule":"...", "execution_mode":"inline|isolated", "timezone":"..."}]}
```
Do not edit HEARTBEAT.md directly in reflection mode.
If not, reply with `HEARTBEAT_OK`.

### Rules
1. You may act directly for routine CRUD operations only.
2. For destructive operations (delete, permission/system/network changes), ask for user confirmation first.
3. New routines should start from the next schedule cycle.
4. Avoid duplicates by checking existing routines first (use `routine_cli.py list`).
5. Use `routine_cli.py` CLI commands (via `_bash`) to manage routines. Never edit HEARTBEAT.md directly.
"""

    def __init__(
        self,
        agent_name: str,
        workspace_path: Path,
        session_manager: "SessionManager",
        agent_factory: Callable,
        interval_minutes: int = 30,
        active_hours: tuple = (8, 22),
        max_retries: int = 3,
        ack_max_chars: int = 300,
        realtime_status_hint: bool = True,
        broadcast_scope: str = "agent",
        routine_reflection: bool = True,
        auto_register_routines: bool = False,
        on_result: Optional[Callable[[str, str], Any]] = None,
        summary_max_chars: Optional[int] = None,
        heartbeat_max_history: int = 10,
        reflect_force_interval_hours: int = 24,
    ):
        """
        初始化心跳运行器

        Args:
            agent_name: Agent 名称
            workspace_path: Agent 工作区路径
            session_manager: Session 管理器
            agent_factory: Agent 创建工厂函数
            interval_minutes: 心跳间隔（分钟）
            active_hours: 活跃时段（开始小时, 结束小时）
            max_retries: 最大重试次数
            ack_max_chars: suppress 阈值（去掉 HEARTBEAT_OK 后剩余长度）
            realtime_status_hint: 是否广播"有后台更新"状态提示
            broadcast_scope: Event broadcast scope, one of: session | agent
            routine_reflection: Whether heartbeat reflection phase is enabled
            on_result: 结果回调函数
            heartbeat_max_history: Max history messages to restore for heartbeat sessions (saves tokens)
            reflect_force_interval_hours: Force reflect even if files unchanged after this many hours
        """
        self.agent_name = agent_name
        self.workspace_path = Path(workspace_path)
        self.session_manager = session_manager
        self.agent_factory = agent_factory
        self.interval_minutes = interval_minutes
        self.active_hours = active_hours
        self.max_retries = max_retries
        self.ack_max_chars = max(0, int(ack_max_chars or 0))
        self.realtime_status_hint = bool(realtime_status_hint)
        scope = str(broadcast_scope or "agent").strip().lower()
        self.broadcast_scope = scope if scope in {"session", "agent"} else "agent"
        self.routine_reflection = bool(routine_reflection)
        self.auto_register_routines = bool(auto_register_routines)
        self.on_result = on_result
        self._heartbeat_max_history = max(1, int(heartbeat_max_history or 10))
        self._reflect_force_interval = timedelta(hours=max(1, int(reflect_force_interval_hours or 24)))
        self._last_reflect_at: Optional[datetime] = None
        self._last_reflect_file_hashes: Dict[str, str] = {}
        if summary_max_chars is not None:
            self.SUMMARY_MAX_CHARS = max(1, int(summary_max_chars))

        self._running = False
        self._last_result: Optional[str] = None
        self._task_list: Optional[TaskList] = None
        self._current_run_id: Optional[str] = None
        self._last_parse_result: Optional[ParseResult] = None
        self._heartbeat_mode: str = "idle"
        self._runtime_workspace_instructions: str = ""
        self._turn_executor = TurnExecutor(
            RuntimeDeps(
                load_workspace_instructions=self._runtime_load_workspace_instructions,
                list_due_tasks=self._runtime_list_due_tasks,
                heartbeat_instructions=self.HEARTBEAT_SYSTEM_INSTRUCTION.strip(),
            )
        )

    def _write_heartbeat_event(self, event_type: str, **kwargs: Any) -> None:
        """Write a structured heartbeat event to the JSONL events file."""
        try:
            user_data = UserDataManager()
            events_file = user_data.heartbeat_events_file
            events_file.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "timestamp": datetime.now().isoformat(),
                "agent": self.agent_name,
                "event": event_type,
            }
            event.update(kwargs)
            with open(events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("Failed to write heartbeat event: %s", exc)

    @property
    def session_id(self) -> str:
        """Execution session id for heartbeat runtime."""
        return self.heartbeat_session_id

    @property
    def primary_session_id(self) -> str:
        """Primary user-facing chat session id."""
        return self.session_manager.get_primary_session_id(self.agent_name)

    @property
    def heartbeat_session_id(self) -> str:
        """Heartbeat-only execution session id."""
        if hasattr(self.session_manager, "get_heartbeat_session_id"):
            return self.session_manager.get_heartbeat_session_id(self.agent_name)
        return f"heartbeat_session_{self.agent_name}"

    def _is_active_time(self) -> bool:
        """检查是否在活跃时段"""
        hour = datetime.now().hour
        start, end = self.active_hours
        return start <= hour < end

    def _compute_file_hashes(self) -> Dict[str, str]:
        """Compute MD5 hashes for MEMORY.md and HEARTBEAT.md."""
        hashes: Dict[str, str] = {}
        for name in ("MEMORY.md", "HEARTBEAT.md"):
            path = self.workspace_path / name
            try:
                if path.exists():
                    hashes[name] = hashlib.md5(path.read_bytes()).hexdigest()
                else:
                    hashes[name] = ""
            except Exception:
                hashes[name] = ""
        return hashes

    def _should_skip_reflection(self) -> bool:
        """Check whether reflection can be skipped.

        Skip when MEMORY.md and HEARTBEAT.md are unchanged since last
        reflect AND the force interval has not elapsed.
        """
        now = datetime.now()
        current_hashes = self._compute_file_hashes()

        # Force reflect if we've never reflected or force interval elapsed
        if self._last_reflect_at is None:
            return False
        if (now - self._last_reflect_at) >= self._reflect_force_interval:
            return False

        # Skip if files haven't changed
        if current_hashes == self._last_reflect_file_hashes:
            return True

        return False

    def _update_reflect_state(self) -> None:
        """Record state after a successful reflect LLM call."""
        self._last_reflect_at = datetime.now()
        self._last_reflect_file_hashes = self._compute_file_hashes()

    def _read_heartbeat_md(self) -> Optional[str]:
        """Read HEARTBEAT.md and decide heartbeat execution mode."""
        path = self.workspace_path / "HEARTBEAT.md"
        if not path.exists():
            self._task_list = None
            self._last_parse_result = ParseResult(status=ParseStatus.EMPTY)
            self._heartbeat_mode = "idle"
            return None

        try:
            content = path.read_text(encoding="utf-8")
            parse_result = parse_heartbeat_md(content)
            self._last_parse_result = parse_result

            if parse_result.status == ParseStatus.OK and parse_result.task_list is not None:
                task_list = parse_result.task_list
                self._task_list = task_list
                due = get_due_tasks(task_list)
                if due:
                    self._heartbeat_mode = "structured_due"
                    return content
                self._heartbeat_mode = "structured_reflect"
                return content

            if parse_result.status == ParseStatus.CORRUPTED:
                self._task_list = None
                self._heartbeat_mode = "corrupted"
                return content

            # No structured JSON block found — treat as idle
            self._task_list = None
            self._heartbeat_mode = "idle"
            return None
        except Exception as e:
            logger.error("Failed to read HEARTBEAT.md: %s", e)
            self._task_list = None
            self._last_parse_result = None
            self._heartbeat_mode = "idle"
            return None

    def _should_deliver(self, response: str) -> bool:
        """判断心跳结果是否应推送给用户。

        规则（参考 OpenClaw HEARTBEAT_OK 机制）：
        - 不含 HEARTBEAT_OK → deliver
        - HEARTBEAT_OK 在开头或结尾，且剩余内容 ≤ ACK_MAX_CHARS → suppress
        - HEARTBEAT_OK 在开头或结尾，但剩余内容 > ACK_MAX_CHARS → deliver
        """
        stripped = response.strip()
        token = "HEARTBEAT_OK"

        if token not in stripped:
            return True

        # 检查 token 是否在开头或结尾
        if stripped.startswith(token):
            remaining = stripped[len(token):].strip()
        elif stripped.endswith(token):
            remaining = stripped[:-len(token)].strip()
        else:
            # token 在中间，不视为 ack 信号 → deliver
            return True

        return len(remaining) > self.ack_max_chars

    def _should_skip_response(self, response: str) -> bool:
        """判断是否静默处理（向后兼容，内部调用 _should_deliver）"""
        return not self._should_deliver(response)

    def _bind_session_id_to_context(self, agent: Any) -> None:
        """Bind session id to context to keep context-engine logs traceable."""
        context = agent.executor.context
        context.set_variable("session_id", self.session_id)
        if hasattr(context, "set_session_id"):
            context.set_session_id(self.session_id)

    def _record_timeline_event(self, event_type: str, run_id: str, **payload: Any) -> None:
        """Append heartbeat timeline event with source metadata."""
        if not hasattr(self.session_manager, "append_timeline_event"):
            return
        event = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "source_type": "heartbeat",
            "run_id": run_id,
        }
        event.update(payload)
        self.session_manager.append_timeline_event(self.session_id, event)

    def _record_runtime_metric(self, name: str, delta: float = 1.0) -> None:
        """Record one runtime metric when session manager supports it."""
        record = getattr(self.session_manager, "record_metric", None)
        if callable(record):
            record(name, delta)

    def _write_heartbeat_file(self, content: str) -> None:
        """Persist HEARTBEAT.md atomically with .bak rotation."""
        hb_path = self.workspace_path / "HEARTBEAT.md"
        SessionPersistence.atomic_save(hb_path, content.encode("utf-8"))

    def _snapshot_path(self) -> Path:
        return self.workspace_path / ".heartbeat_snapshot.json"

    def _write_task_snapshot(self, task_list: TaskList) -> None:
        """Persist latest parsed task list as recovery snapshot."""
        payload = {
            "saved_at": datetime.now().isoformat(),
            "task_list": task_list.to_dict(),
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        SessionPersistence.atomic_save(self._snapshot_path(), serialized)

    def _load_task_snapshot(self) -> Optional[dict]:
        """Load task snapshot for corruption-repair context."""
        snapshot_path = self._snapshot_path()
        if not snapshot_path.exists():
            return None
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read heartbeat snapshot: %s", exc)
            return None

    def _render_snapshot_summary(self) -> str:
        """Render compact task summary from snapshot for prompt injection."""
        snapshot = self._load_task_snapshot()
        if not snapshot:
            return "(no snapshot available)"
        task_list = snapshot.get("task_list", {})
        tasks = task_list.get("tasks", []) if isinstance(task_list, dict) else []
        if not tasks:
            return "(snapshot exists but contains no tasks)"
        lines = []
        for task in tasks[:10]:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id", "unknown")
            title = task.get("title", "")
            schedule = task.get("schedule", "")
            lines.append(f"- {task_id}: {title} ({schedule})")
        return "\n".join(lines) if lines else "(snapshot has no valid tasks)"

    def _merge_heartbeat_instruction(self, current_instruction: str) -> str:
        """Inject heartbeat instruction block once to avoid prompt growth."""
        current = current_instruction or ""
        block_pattern = re.compile(
            rf"{re.escape(self._HEARTBEAT_INST_START)}.*?{re.escape(self._HEARTBEAT_INST_END)}",
            re.DOTALL,
        )
        cleaned = block_pattern.sub("", current).strip()
        hb_block = (
            f"{self._HEARTBEAT_INST_START}\n"
            f"{self.HEARTBEAT_SYSTEM_INSTRUCTION.strip()}\n"
            f"{self._HEARTBEAT_INST_END}"
        )
        if not cleaned:
            return hb_block
        return f"{cleaned}\n\n{hb_block}"

    @staticmethod
    def _get_workspace_instructions(context: Any) -> str:
        """Read workspace instructions from context safely."""
        get_var = getattr(context, "get_var_value", None)
        if callable(get_var):
            value = get_var("workspace_instructions")
            return value if isinstance(value, str) else ""
        return ""

    def _build_heartbeat_system_prompt(self, context: Any) -> str:
        """Build per-turn heartbeat system prompt without mutating context."""
        base = self._get_workspace_instructions(context)
        return self._merge_heartbeat_instruction(base)

    @staticmethod
    def _extract_reflection_routine_proposals(response: str) -> list[dict[str, Any]]:
        """Extract routine proposals from reflection response JSON payload."""
        if not isinstance(response, str) or not response.strip():
            return []

        def _from_payload(payload: Any) -> list[dict[str, Any]]:
            if not isinstance(payload, dict):
                return []
            routines = payload.get("routines")
            if not isinstance(routines, list):
                return []
            return [item for item in routines if isinstance(item, dict)]

        for match in re.finditer(r"```json\s*(\{.*?\})\s*```", response, re.DOTALL):
            try:
                payload = json.loads(match.group(1))
            except Exception:
                continue
            proposals = _from_payload(payload)
            if proposals:
                return proposals

        try:
            payload = json.loads(response.strip())
        except Exception:
            return []
        return _from_payload(payload)

    @staticmethod
    def _normalize_reflection_routine(item: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Normalize one routine proposal into RoutineManager add payload."""
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        execution_mode = str(item.get("execution_mode") or "auto").strip().lower()
        if execution_mode not in {"inline", "isolated", "auto"}:
            execution_mode = "auto"
        description = str(item.get("description") or "").strip()
        schedule_raw = item.get("schedule")
        schedule = None
        if schedule_raw is not None:
            schedule_text = str(schedule_raw).strip()
            schedule = schedule_text or None
        timezone_name = item.get("timezone")
        if timezone_name is not None:
            timezone_name = str(timezone_name).strip() or None
        timeout_seconds = item.get("timeout_seconds", 120)
        try:
            timeout_seconds = max(1, int(timeout_seconds))
        except Exception:
            timeout_seconds = 120
        return {
            "title": title,
            "description": description,
            "schedule": schedule,
            "execution_mode": execution_mode,
            "timezone_name": timezone_name,
            "timeout_seconds": timeout_seconds,
            "source": "heartbeat_reflect",
            "allow_duplicate": False,
        }

    def _apply_reflection_routine_proposals(self, response: str, run_id: str) -> str:
        """Apply reflection-proposed routines through framework-side strong constraints."""
        proposals = self._extract_reflection_routine_proposals(response)
        if not proposals:
            return response

        manager = RoutineManager(self.workspace_path)
        added: list[dict[str, Any]] = []
        skipped_duplicates = 0
        failed = 0

        for raw in proposals:
            normalized = self._normalize_reflection_routine(raw)
            if normalized is None:
                continue
            try:
                created = manager.add_routine(**normalized)
                added.append(created)
            except ValueError as exc:
                detail = str(exc)
                if "duplicate routine" in detail or "task_id already exists" in detail:
                    skipped_duplicates += 1
                else:
                    failed += 1
                    logger.warning(
                        "Reflection routine apply rejected: agent=%s run_id=%s title=%s reason=%s",
                        self.agent_name,
                        run_id,
                        normalized.get("title", ""),
                        detail,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Reflection routine apply failed: agent=%s run_id=%s title=%s error=%s",
                    self.agent_name,
                    run_id,
                    normalized.get("title", ""),
                    str(exc),
                )

        if not added:
            return response

        # Refresh in-memory task snapshot after out-of-band task file updates.
        self._read_heartbeat_md()

        lines = [f"Registered {len(added)} routine(s) from heartbeat reflection."]
        for item in added[:5]:
            title = str(item.get("title") or "")
            schedule = str(item.get("schedule") or "manual")
            lines.append(f"- {title} ({schedule})")
        if skipped_duplicates > 0:
            lines.append(f"Skipped duplicates: {skipped_duplicates}.")
        if failed > 0:
            lines.append(f"Failed to apply: {failed}.")
        return "\n".join(lines)

    async def _deposit_routine_proposals_to_mailbox(
        self, proposals: list[dict[str, Any]], run_id: str,
    ) -> str:
        """Deposit routine proposals to the primary session mailbox for user review."""
        lines = [f"Heartbeat reflection proposed {len(proposals)} routine(s) for review:"]
        normalized_list: list[dict[str, Any]] = []
        for raw in proposals:
            normalized = self._normalize_reflection_routine(raw)
            if normalized is None:
                continue
            normalized_list.append(normalized)
            title = normalized.get("title", "")
            schedule = normalized.get("schedule") or "manual"
            lines.append(f"- {title} ({schedule})")

        summary = "\n".join(lines)
        event = build_system_event(
            event_type="routine_proposal",
            source_session_id=self.session_id,
            summary=summary[:self.SUMMARY_MAX_CHARS],
            detail=json.dumps(normalized_list, ensure_ascii=False, indent=2),
            artifacts=[],
            priority=0,
            suppress_if_stale=False,
            dedupe_key=f"routine_proposal:{self.agent_name}:{run_id}",
        )
        target_session = self.primary_session_id
        if hasattr(self.session_manager, "deposit_mailbox_event"):
            await self.session_manager.deposit_mailbox_event(
                target_session, event, timeout=5.0, blocking=True,
            )
        return summary

    def _runtime_load_workspace_instructions(self, agent_name: str) -> str:
        """Return workspace instructions for runtime strategy lookup."""
        if agent_name and agent_name != self.agent_name:
            return ""
        return self._runtime_workspace_instructions or ""

    def _runtime_list_due_tasks(self, agent_name: str) -> list[dict[str, Any]]:
        """Return due task snapshots for heartbeat context strategy."""
        if agent_name and agent_name != self.agent_name:
            return []
        task_list = self._task_list
        if task_list is None:
            return []
        due = get_due_tasks(task_list)
        snapshots: list[dict[str, Any]] = []
        for task in due:
            snapshot = self._task_snapshot(task)
            if snapshot.get("id"):
                snapshots.append(snapshot)
        return snapshots

    async def _deposit_deliver_event_to_primary_session(self, content: str, run_id: str) -> bool:
        """Deposit heartbeat result into primary-session mailbox as SystemEvent."""
        target_session = self.primary_session_id
        # Use stable dedupe_key based on heartbeat mode (not run_id) so that
        # same-type heartbeat results auto-deduplicate in the mailbox.
        mode = self._heartbeat_mode or "unknown"
        dedupe_key = f"heartbeat:{self.agent_name}:{mode}"
        event = build_system_event(
            event_type="heartbeat_result",
            source_session_id=self.session_id,
            summary=content[:self.SUMMARY_MAX_CHARS],
            detail=content,
            artifacts=[],
            priority=0,
            suppress_if_stale=True,
            dedupe_key=dedupe_key,
        )

        if hasattr(self.session_manager, "deposit_mailbox_event"):
            ok = await self.session_manager.deposit_mailbox_event(
                target_session,
                event,
                timeout=5.0,
                blocking=True,
            )
            if not ok:
                logger.warning("[%s] Failed to deposit heartbeat event to mailbox", self.agent_name)
            return ok

        # Backward-compatible fallback for tests/mocks with update_atomic only.
        def _mutator(session_data: Any) -> None:
            mailbox = getattr(session_data, "mailbox", None)
            if not isinstance(mailbox, list):
                session_data.mailbox = []
            session_data.mailbox.append(dict(event))

        updated = await self.session_manager.update_atomic(
            target_session,
            _mutator,
            timeout=5.0,
            blocking=True,
        )
        return updated is not None

    async def _inject_result_to_primary_history(self, result: str, run_id: str) -> bool:
        """Inject heartbeat result as an assistant message in primary session history."""
        message = {
            "role": "assistant",
            "content": result,
            "metadata": {
                "source": "heartbeat",
                "run_id": run_id,
                "injected_at": datetime.now().isoformat(),
            },
        }
        if hasattr(self.session_manager, "inject_history_message"):
            ok = await self.session_manager.inject_history_message(
                self.primary_session_id,
                message,
                timeout=5.0,
                blocking=True,
            )
            if not ok:
                logger.warning("[%s] Failed to inject heartbeat result to history", self.agent_name)
            return ok
        return False

    def _init_session_trajectory(self, agent: Any, overwrite: bool = False) -> None:
        """Initialize session-scoped trajectory file."""
        user_data = UserDataManager()
        trajectory_path = user_data.get_session_trajectory_path(self.agent_name, self.session_id)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        agent.executor.context.init_trajectory(str(trajectory_path), overwrite=overwrite)

    async def _emit_result(self, result: str) -> None:
        """Dispatch heartbeat result to callback supporting sync/async handlers."""
        if not self.on_result:
            return
        callback_result = self.on_result(self.agent_name, result)
        if inspect.isawaitable(callback_result):
            await callback_result

    async def _execute_with_retry(
        self,
        *,
        include_inline: bool = True,
        include_isolated: bool = True,
    ) -> str:
        """带重试的执行逻辑"""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                execute_once = self._execute_once
                try:
                    sig = inspect.signature(execute_once)
                    kwargs: dict[str, Any] = {}
                    if "include_inline" in sig.parameters:
                        kwargs["include_inline"] = include_inline
                    if "include_isolated" in sig.parameters:
                        kwargs["include_isolated"] = include_isolated
                    if kwargs:
                        return await execute_once(**kwargs)
                except (TypeError, ValueError):
                    pass
                return await execute_once()
            except Exception as e:
                last_error = e
                if _is_permanent_error(e):
                    logger.warning(
                        "心跳遇到不可恢复错误，跳过重试 (尝试 %d/%d): %s",
                        attempt + 1, self.max_retries, e,
                    )
                    break
                logger.warning(f"心跳执行失败 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))  # 递增等待

        raise last_error

    async def _execute_once(
        self,
        *,
        include_inline: bool = True,
        include_isolated: bool = True,
    ) -> str:
        """执行一次心跳

        Uses cross-process file lock (flock) to guarantee mutual exclusion
        with ChatService.  If lock is unavailable (chat in progress), skip.
        """
        if hasattr(self.session_manager, "migrate_legacy_sessions_for_agent"):
            await self.session_manager.migrate_legacy_sessions_for_agent(self.agent_name)
        run_id = f"heartbeat_{uuid.uuid4().hex[:12]}"
        self._current_run_id = run_id
        post_message: Optional[dict] = None

        async def _run_locked_body() -> str:
            nonlocal post_message
            self._record_timeline_event("turn_start", run_id, trigger="heartbeat")

            # 2. Pre-check: read HEARTBEAT.md BEFORE creating agent to skip
            #    unnecessary agent creation / session I/O for idle ticks.
            try:
                heartbeat_content = self._read_heartbeat_md()
                if not heartbeat_content:
                    self._write_heartbeat_event("idle")
                    self._record_timeline_event("turn_end", run_id, status="completed", result="HEARTBEAT_IDLE")
                    from .events import emit
                    await emit(self.primary_session_id, {"type": "status", "content": "心跳预检：暂无待办任务"},
                               agent_name=self.agent_name, scope=self.broadcast_scope,
                               source_type="heartbeat", run_id=run_id)
                    await asyncio.sleep(1)
                    await emit(self.primary_session_id, {"type": "status", "content": ""},
                               agent_name=self.agent_name, scope=self.broadcast_scope,
                               source_type="heartbeat", run_id=run_id)
                    return "HEARTBEAT_IDLE"

                # Skip agent creation for reflection when not needed
                if self._heartbeat_mode == "structured_reflect":
                    if not self.routine_reflection:
                        logger.info("[%s] Routine reflection disabled by config, skipping agent creation", self.agent_name)
                        self._write_heartbeat_event("reflect_skipped", reason="disabled")
                        self._record_timeline_event("turn_end", run_id, status="completed", result="HEARTBEAT_OK")
                        return "HEARTBEAT_OK"
                    if self._should_skip_reflection():
                        logger.info("[%s] Reflection skipped: files unchanged since last reflect", self.agent_name)
                        self._write_heartbeat_event("reflect_skipped", reason="file_unchanged")
                        self._record_timeline_event("turn_end", run_id, status="completed", result="HEARTBEAT_OK")
                        self._record_runtime_metric("heartbeat_reflect_skipped")
                        return "HEARTBEAT_OK"

                logger.info("[%s] heartbeat mode=%s", self.agent_name, self._heartbeat_mode)

                # 3. Only create agent when LLM call is actually needed
                agent = await self._get_or_create_agent()

                if self._heartbeat_mode == "structured_due" and self._task_list is not None:
                    result = await self._execute_structured_tasks(
                        agent,
                        heartbeat_content,
                        run_id,
                        include_inline=include_inline,
                        include_isolated=include_isolated,
                    )
                elif self._heartbeat_mode == "structured_reflect":
                    user_message = await self._inject_heartbeat_context(
                        agent,
                        heartbeat_content,
                        mode="reflect",
                    )
                    result = await self._run_agent(agent, user_message)
                    self._update_reflect_state()
                    proposals = self._extract_reflection_routine_proposals(result)
                    if proposals:
                        if self.auto_register_routines:
                            result = self._apply_reflection_routine_proposals(result, run_id)
                        else:
                            result = await self._deposit_routine_proposals_to_mailbox(proposals, run_id)
                        self._write_heartbeat_event("reflect", result="proposals", proposal_count=len(proposals))
                    else:
                        # No actionable routine proposals found — suppress to
                        # prevent prompt-echo from polluting the primary mailbox.
                        logger.debug("[%s] Reflection produced no proposals, forcing HEARTBEAT_OK", self.agent_name)
                        self._write_heartbeat_event("reflect", result="ok")
                        result = "HEARTBEAT_OK"
                elif self._heartbeat_mode == "corrupted":
                    self._write_heartbeat_event("corrupted")
                    user_message = await self._inject_heartbeat_context(
                        agent,
                        heartbeat_content,
                        mode="corrupted",
                    )
                    result = await self._run_agent(agent, user_message)
                else:
                    logger.warning(
                        "[%s] Unexpected heartbeat mode=%s, skipping",
                        self.agent_name, self._heartbeat_mode,
                    )
                    result = "HEARTBEAT_OK"

                deliver = self._should_deliver(result)

                # Save while lock is held to prevent stale-agent overwrite.
                await self._save_session_atomic(agent)

                if deliver:
                    await self._inject_result_to_primary_history(result, run_id)
                    await self._deposit_deliver_event_to_primary_session(result, run_id)
                if deliver and self.realtime_status_hint:
                    post_message = {
                        "type": "message",
                        "role": "assistant",
                        "content": result,
                        "summary": result[:self.SUMMARY_MAX_CHARS],
                        "detail": result,
                        "source_type": "heartbeat_delivery",
                        "run_id": run_id,
                        "deliver": True,
                    }
                if deliver:
                    self._record_runtime_metric("heartbeat_deliver_count")
                    logger.info("[%s] Heartbeat result delivered to user", self.agent_name)
                else:
                    self._record_runtime_metric("heartbeat_suppress_count")
                    logger.debug("[%s] Heartbeat result suppressed (HEARTBEAT_OK)", self.agent_name)
                if self._task_list is not None:
                    self._write_task_snapshot(self._task_list)
                self._record_timeline_event("turn_end", run_id, status="completed", result=result)
                return result
            except Exception as e:
                self._record_timeline_event("turn_end", run_id, status="error", error=str(e))
                raise

        # 1) In-process lock first (prevents same-process coroutine races)
        inproc_acquired = False
        result = "HEARTBEAT_SKIPPED"
        if hasattr(self.session_manager, "acquire_session") and hasattr(self.session_manager, "release_session"):
            inproc_acquired = await self.session_manager.acquire_session(self.session_id, timeout=0.1)
            if not inproc_acquired:
                self._record_runtime_metric("heartbeat_skipped_due_to_lock")
                self._write_heartbeat_event("skipped", reason="locked")
                return "HEARTBEAT_SKIPPED"
        try:
            # 2) Cross-process lock (daemon vs web)
            if hasattr(self.session_manager, "file_lock"):
                with self.session_manager.file_lock(self.session_id, blocking=False) as acquired:
                    if not acquired:
                        logger.info("[%s] Session locked by another process, skipping heartbeat", self.agent_name)
                        self._record_runtime_metric("heartbeat_skipped_due_to_lock")
                        self._write_heartbeat_event("skipped", reason="locked")
                        return "HEARTBEAT_SKIPPED"
                    result = await _run_locked_body()

            # Compatibility fallback for tests/mocks that only provide session_context.
            elif hasattr(self.session_manager, "session_context"):
                async with self.session_manager.session_context(self.session_id, timeout=5.0) as acquired:
                    if not acquired:
                        self._record_runtime_metric("heartbeat_skipped_due_to_lock")
                        self._write_heartbeat_event("skipped", reason="locked")
                        return "HEARTBEAT_SKIPPED"
                    result = await _run_locked_body()

            else:
                result = await _run_locked_body()
        finally:
            if inproc_acquired:
                self.session_manager.release_session(self.session_id)

        if post_message is not None:
            from .events import emit

            await emit(
                self.primary_session_id,
                post_message,
                agent_name=self.agent_name,
                scope=self.broadcast_scope,
                source_type="heartbeat_delivery",
                run_id=run_id,
            )
        return result

    async def _save_session_atomic(self, agent: Any):
        """Save session using atomic protocol: already inside flock, so just save.

        Since _execute_once holds the file lock for the entire tick,
        we can safely save without re-acquiring.
        """
        await self.session_manager.save_session(
            self.session_id,
            agent,
            lock_already_held=True,
        )

    def _flush_task_state(self) -> None:
        """Persist current task_list state to HEARTBEAT.md atomically."""
        task_list = self._task_list
        if task_list is None:
            return
        hb_path = self.workspace_path / "HEARTBEAT.md"
        try:
            purged = purge_stale_tasks(task_list)
            if purged:
                logger.info("Purged %d stale task(s) from HEARTBEAT.md", purged)
            content = hb_path.read_text(encoding="utf-8")
            updated = write_task_block(content, task_list)
            self._write_heartbeat_file(updated)
        except Exception as exc:
            logger.warning("Failed to update HEARTBEAT.md task state: %s", exc)

    @staticmethod
    def _task_snapshot(task: Any) -> dict[str, Any]:
        """Build one lightweight task snapshot for scheduler handoff."""
        return {
            "id": str(getattr(task, "id", "")),
            "title": str(getattr(task, "title", "")),
            "description": str(getattr(task, "description", "") or ""),
            "execution_mode": str(getattr(task, "execution_mode", "inline") or "inline"),
            "timeout_seconds": int(getattr(task, "timeout_seconds", 120) or 120),
            "schedule": getattr(task, "schedule", None),
            "timezone": getattr(task, "timezone", None),
        }

    def list_due_isolated_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due isolated tasks for external scheduler routing."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._task_list is None:
            return []
        due = get_due_tasks(self._task_list, now=now)
        isolated = []
        for task in due:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode != "isolated":
                continue
            snapshot = self._task_snapshot(task)
            if snapshot["id"]:
                isolated.append(snapshot)
        return isolated

    def list_due_inline_tasks(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """List due inline tasks for external scheduler routing."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._task_list is None:
            return []
        due = get_due_tasks(self._task_list, now=now)
        inline = []
        for task in due:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode == "isolated":
                continue
            snapshot = self._task_snapshot(task)
            if snapshot["id"]:
                inline.append(snapshot)
        return inline

    def _claim_isolated_task_under_lock(self, task_id: str, now: Optional[datetime] = None) -> bool:
        """Claim one isolated task while heartbeat lock is held."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._task_list is None:
            return False
        due = get_due_tasks(self._task_list, now=now)
        for task in due:
            mode = str(getattr(task, "execution_mode", "inline") or "inline")
            if mode != "isolated":
                continue
            if str(getattr(task, "id", "")) != task_id:
                continue
            if not claim_task(task, now=now):
                return False
            self._flush_task_state()
            return True
        return False

    async def claim_isolated_task(self, task_id: str, now: Optional[datetime] = None) -> bool:
        """Claim one isolated task with heartbeat session lock protection."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return False

        inproc_acquired = False
        if hasattr(self.session_manager, "acquire_session") and hasattr(self.session_manager, "release_session"):
            inproc_acquired = await self.session_manager.acquire_session(self.session_id, timeout=0.1)
            if not inproc_acquired:
                return False
        try:
            if hasattr(self.session_manager, "file_lock"):
                with self.session_manager.file_lock(self.session_id, blocking=False) as acquired:
                    if not acquired:
                        return False
                    return self._claim_isolated_task_under_lock(task_id, now=now)
            if hasattr(self.session_manager, "session_context"):
                async with self.session_manager.session_context(self.session_id, timeout=5.0) as acquired:
                    if not acquired:
                        return False
                    return self._claim_isolated_task_under_lock(task_id, now=now)
            return self._claim_isolated_task_under_lock(task_id, now=now)
        finally:
            if inproc_acquired:
                self.session_manager.release_session(self.session_id)

    async def _update_isolated_task_state(
        self,
        task_id: str,
        state: TaskState,
        *,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Update one isolated task state under heartbeat lock and flush file."""
        inproc_acquired = False
        if hasattr(self.session_manager, "acquire_session") and hasattr(self.session_manager, "release_session"):
            inproc_acquired = await self.session_manager.acquire_session(self.session_id, timeout=5.0)
            if not inproc_acquired:
                return
        try:
            if hasattr(self.session_manager, "file_lock"):
                with self.session_manager.file_lock(self.session_id, blocking=True) as acquired:
                    if not acquired:
                        return
                    self._apply_isolated_task_state_under_lock(
                        task_id, state, error_message=error_message, now=now,
                    )
                    return
            if hasattr(self.session_manager, "session_context"):
                async with self.session_manager.session_context(self.session_id, timeout=5.0) as acquired:
                    if not acquired:
                        return
                    self._apply_isolated_task_state_under_lock(
                        task_id, state, error_message=error_message, now=now,
                    )
                    return
            self._apply_isolated_task_state_under_lock(
                task_id, state, error_message=error_message, now=now,
            )
        finally:
            if inproc_acquired:
                self.session_manager.release_session(self.session_id)

    def _apply_isolated_task_state_under_lock(
        self,
        task_id: str,
        state: TaskState,
        *,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Apply isolated-task state change while heartbeat lock is held."""
        heartbeat_content = self._read_heartbeat_md()
        if not heartbeat_content or self._task_list is None:
            return
        for task in self._task_list.tasks:
            if str(getattr(task, "id", "")) != task_id:
                continue
            update_task_state(task, state, error_message=error_message, now=now)
            self._flush_task_state()
            return

    async def execute_isolated_claimed_task(
        self,
        task_snapshot: dict[str, Any],
        *,
        run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Execute one already-claimed isolated task and persist final state."""
        task_id = str(task_snapshot.get("id") or "").strip()
        if not task_id:
            return
        try:
            task = Task.from_dict(task_snapshot)
            task.execution_mode = "isolated"
            active_run_id = run_id or f"heartbeat_isolated_{uuid.uuid4().hex[:12]}"
            await self._execute_isolated_task(task, active_run_id)
            await self._update_isolated_task_state(task_id, TaskState.DONE, now=now)
        except Exception as exc:
            await self._update_isolated_task_state(
                task_id,
                TaskState.FAILED,
                error_message=str(exc),
                now=now,
            )
            raise

    # ── Deterministic tasks (no LLM, programmatic output) ────────

    @staticmethod
    def _is_time_reminder_task(task: Any) -> bool:
        """Identify time reminder tasks by id/title/description heuristics."""
        parts = []
        for attr in ("id", "title", "description"):
            value = getattr(task, attr, "")
            if isinstance(value, str) and value.strip():
                parts.append(value.lower())
        if not parts:
            return False
        joined = " ".join(parts)
        markers = (
            "time_reminder",
            "time reminder",
            "当前时间",
            "报时",
        )
        return any(marker in joined for marker in markers)

    def _try_deterministic_task(self, task: Any) -> Optional[str]:
        """Return programmatic output for deterministic tasks, or None to fall through to LLM."""
        if self._is_time_reminder_task(task):
            return f"当前时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\nHEARTBEAT_OK"
        return None

    @staticmethod
    def _build_job_session_id(task: Any) -> str:
        """Build one isolated job session id."""
        task_id = str(getattr(task, "id", "task"))
        return f"job_{task_id}_{uuid.uuid4().hex[:8]}"

    async def _create_job_agent(self, job_session_id: str) -> Any:
        """Create a fresh agent for isolated job execution."""
        agent = await self.agent_factory(self.agent_name, self.workspace_path)
        context = agent.executor.context
        context.set_variable("session_id", job_session_id)
        if hasattr(context, "set_session_id"):
            context.set_session_id(job_session_id)
        context.set_variable("job_session_id", job_session_id)
        user_data = UserDataManager()
        trajectory_path = user_data.get_session_trajectory_path(self.agent_name, job_session_id)
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        context.init_trajectory(str(trajectory_path), overwrite=True)
        return agent

    def _build_job_system_prompt(self, agent: Any, task: Any) -> str:
        """Build one isolated job system prompt from base workspace + task description."""
        context = agent.executor.context
        base = self._get_workspace_instructions(context)
        task_description = str(getattr(task, "description", "") or "").strip()
        if not task_description:
            return base
        if not base:
            return task_description
        return f"{base}\n\n{task_description}"

    async def _deposit_job_event_to_primary_session(
        self,
        *,
        event_type: str,
        source_session_id: str,
        summary: str,
        detail: Optional[str],
        run_id: str,
    ) -> bool:
        """Deposit isolated job result to primary mailbox."""
        target_session = self.primary_session_id
        event = build_system_event(
            event_type=event_type,
            source_session_id=source_session_id,
            summary=summary[:self.SUMMARY_MAX_CHARS],
            detail=detail,
            artifacts=[],
            priority=0,
            suppress_if_stale=False,
            dedupe_key=f"{event_type}:{self.agent_name}:{run_id}:{source_session_id}",
        )
        if hasattr(self.session_manager, "deposit_mailbox_event"):
            return await self.session_manager.deposit_mailbox_event(
                target_session,
                event,
                timeout=5.0,
                blocking=True,
            )
        return False

    async def _execute_isolated_task(self, task: Any, run_id: str) -> str:
        """Execute one isolated task with a dedicated job session and agent."""
        job_session_id = self._build_job_session_id(task)
        self._record_runtime_metric("job_session_created")
        task_title = str(getattr(task, "title", "") or "")
        task_desc = str(getattr(task, "description", "") or "")
        prompt = (
            "Execute this scheduled isolated routine task and summarize the result briefly.\n\n"
            f"Task ID: {getattr(task, 'id', 'task')}\n"
            f"Title: {task_title}\n"
            f"Description: {task_desc}\n"
        )
        agent = await self._create_job_agent(job_session_id)
        job_system_prompt = self._build_job_system_prompt(agent, task)
        try:
            result = await asyncio.wait_for(
                self._run_agent(agent, prompt, system_prompt_override=job_system_prompt),
                timeout=getattr(task, "timeout_seconds", 120),
            )
            await self.session_manager.save_session(job_session_id, agent)
            if hasattr(self.session_manager, "mark_session_archived"):
                await self.session_manager.mark_session_archived(job_session_id)
            summary = f"{task_title or getattr(task, 'id', 'task')} completed"
            await self._deposit_job_event_to_primary_session(
                event_type="job_completed",
                source_session_id=job_session_id,
                summary=summary,
                detail=result,
                run_id=run_id,
            )
            # Persist the actual result into primary session history so it
            # survives page refreshes (mailbox events are ACKed on drain).
            await self._inject_result_to_primary_history(result, run_id)
            # Emit heartbeat_delivery event so Telegram (and other
            # subscribers) push the result in real time.
            from .events import emit

            await emit(
                self.primary_session_id,
                {
                    "type": "message",
                    "role": "assistant",
                    "content": result,
                    "summary": result[:self.SUMMARY_MAX_CHARS],
                    "detail": result,
                    "source_type": "heartbeat_delivery",
                    "run_id": run_id,
                    "deliver": True,
                },
                agent_name=self.agent_name,
                scope=self.broadcast_scope,
                source_type="heartbeat_delivery",
                run_id=run_id,
            )
            return result
        except Exception as exc:
            await self.session_manager.save_session(job_session_id, agent)
            if hasattr(self.session_manager, "mark_session_archived"):
                await self.session_manager.mark_session_archived(job_session_id)
            summary = f"{task_title or getattr(task, 'id', 'task')} failed"
            await self._deposit_job_event_to_primary_session(
                event_type="job_failed",
                source_session_id=job_session_id,
                summary=summary,
                detail=str(exc),
                run_id=run_id,
            )
            # Emit heartbeat_delivery for the failure path too, so users
            # get notified of task failures via Telegram.
            from .events import emit

            _fail_msg = f"Task failed: {task_title or getattr(task, 'id', 'task')}\n{exc}"
            await emit(
                self.primary_session_id,
                {
                    "type": "message",
                    "role": "assistant",
                    "content": _fail_msg,
                    "summary": _fail_msg[:self.SUMMARY_MAX_CHARS],
                    "detail": _fail_msg,
                    "source_type": "heartbeat_delivery",
                    "run_id": run_id,
                    "deliver": True,
                },
                agent_name=self.agent_name,
                scope=self.broadcast_scope,
                source_type="heartbeat_delivery",
                run_id=run_id,
            )
            raise

    async def _execute_structured_tasks(
        self,
        agent: Any,
        heartbeat_content: str,
        run_id: str,
        *,
        include_inline: bool = True,
        include_isolated: bool = True,
    ) -> str:
        """Execute due structured tasks with per-task state tracking."""
        task_list = self._task_list
        if task_list is None:
            return "HEARTBEAT_OK"
        due = get_due_tasks(task_list)
        inline_due = [t for t in due if str(getattr(t, "execution_mode", "inline") or "inline") != "isolated"]
        isolated_due = [t for t in due if str(getattr(t, "execution_mode", "inline") or "inline") == "isolated"]
        results = []

        if include_inline:
            for task in inline_due:
                if not claim_task(task):
                    continue
                # Persist claim before execution so concurrent ticks/processes observe running state.
                self._flush_task_state()
                self._write_heartbeat_event("task_start", task_id=task.id, title=task.title, execution_mode="inline")
                self._record_timeline_event(
                    "task_start", run_id, task_id=task.id, task_title=task.title,
                )

                try:
                    # Deterministic tasks: programmatic output, skip LLM
                    deterministic_result = self._try_deterministic_task(task)
                    if deterministic_result is not None:
                        result = deterministic_result
                        update_task_state(task, TaskState.DONE)
                        self._write_heartbeat_event("task_done", task_id=task.id, title=task.title)
                        self._record_timeline_event(
                            "task_end", run_id,
                            task_id=task.id, status="done", result=result[:self.SUMMARY_MAX_CHARS],
                        )
                        results.append(result)
                        continue

                    user_message = await self._inject_heartbeat_context(
                        agent,
                        heartbeat_content,
                        mode="execute_due",
                        current_task=task,
                    )
                    result = await asyncio.wait_for(
                        self._run_agent(agent, user_message),
                        timeout=task.timeout_seconds,
                    )
                    update_task_state(task, TaskState.DONE)
                    self._write_heartbeat_event("task_done", task_id=task.id, title=task.title)
                    self._record_timeline_event(
                        "task_end", run_id,
                        task_id=task.id, status="done", result=result[:self.SUMMARY_MAX_CHARS],
                    )
                    results.append(result)
                except asyncio.TimeoutError:
                    update_task_state(
                        task, TaskState.FAILED, error_message="timeout",
                    )
                    self._write_heartbeat_event("task_failed", task_id=task.id, title=task.title, error="timeout")
                    self._record_timeline_event(
                        "task_end", run_id,
                        task_id=task.id, status="failed", error="timeout",
                    )
                    results.append(f"TASK_TIMEOUT:{task.id}")
                except Exception as exc:
                    update_task_state(
                        task, TaskState.FAILED, error_message=str(exc),
                    )
                    self._write_heartbeat_event("task_failed", task_id=task.id, title=task.title, error=str(exc)[:200])
                    self._record_timeline_event(
                        "task_end", run_id,
                        task_id=task.id, status="failed", error=str(exc),
                    )
                    results.append(f"TASK_FAILED:{task.id}")

                # Write state after each task to survive crashes
                self._flush_task_state()

        if include_isolated:
            for task in isolated_due:
                if not claim_task(task):
                    continue
                # Persist claim before execution so concurrent ticks/processes observe running state.
                self._flush_task_state()
                self._write_heartbeat_event("task_start", task_id=task.id, title=task.title, execution_mode="isolated")
                self._record_timeline_event(
                    "task_start", run_id, task_id=task.id, task_title=task.title, execution_mode="isolated",
                )
                try:
                    result = await self._execute_isolated_task(task, run_id)
                    update_task_state(task, TaskState.DONE)
                    self._write_heartbeat_event("task_done", task_id=task.id, title=task.title)
                    self._record_timeline_event(
                        "task_end", run_id,
                        task_id=task.id, status="done", execution_mode="isolated", result=result[:self.SUMMARY_MAX_CHARS],
                    )
                    results.append(f"ISOLATED_DONE:{task.id}")
                except asyncio.TimeoutError:
                    update_task_state(task, TaskState.FAILED, error_message="timeout")
                    self._write_heartbeat_event("task_failed", task_id=task.id, title=task.title, error="timeout")
                    self._record_timeline_event(
                        "task_end", run_id,
                        task_id=task.id, status="failed", execution_mode="isolated", error="timeout",
                    )
                    results.append(f"ISOLATED_TIMEOUT:{task.id}")
                except Exception as exc:
                    update_task_state(task, TaskState.FAILED, error_message=str(exc))
                    self._write_heartbeat_event("task_failed", task_id=task.id, title=task.title, error=str(exc)[:200])
                    self._record_timeline_event(
                        "task_end", run_id,
                        task_id=task.id, status="failed", execution_mode="isolated", error=str(exc),
                    )
                    results.append(f"ISOLATED_FAILED:{task.id}")
                self._flush_task_state()

        if not results:
            return "HEARTBEAT_OK"
        # Filter out task status markers — isolated results have already been
        # injected into primary history individually, and inline failure/timeout
        # markers should not leak into user-visible output.
        _STATUS_PREFIXES = (
            "ISOLATED_DONE:", "ISOLATED_TIMEOUT:", "ISOLATED_FAILED:",
            "TASK_FAILED:", "TASK_TIMEOUT:",
        )
        meaningful = [r for r in results if not any(r.startswith(p) for p in _STATUS_PREFIXES)]
        return "; ".join(meaningful) if meaningful else "HEARTBEAT_OK"

    def _trim_session_history(self, session_data: Any) -> None:
        """Trim session history_messages in-place to reduce token usage.

        Uses heartbeat_max_history for reflect/idle modes, and a higher limit
        for structured_due mode where more context helps task execution.
        """
        if session_data is None:
            return
        messages = getattr(session_data, "history_messages", None)
        if not messages:
            return
        if self._heartbeat_mode == "structured_due":
            max_history = max(self._heartbeat_max_history, 30)
        else:
            max_history = self._heartbeat_max_history
        original_len = len(messages)
        if original_len > max_history:
            session_data.history_messages = messages[-max_history:]
            logger.info(
                "[%s] Trimmed heartbeat session history from %d to %d messages",
                self.agent_name, original_len, max_history,
            )

    async def _get_or_create_agent(self) -> Any:
        """获取或创建 Agent"""
        session_data = None
        if hasattr(self.session_manager, "load_session"):
            session_data = await self.session_manager.load_session(self.session_id)

        # Trim history before restore to reduce token consumption
        self._trim_session_history(session_data)

        # 先从缓存获取
        agent = self.session_manager.get_cached_agent(self.session_id)
        if agent:
            logger.debug(f"从缓存获取 Agent: {self.session_id}")
            if session_data:
                if hasattr(self.session_manager, "restore_timeline"):
                    self.session_manager.restore_timeline(self.session_id, session_data.timeline or [])
                if hasattr(self.session_manager, "restore_to_agent"):
                    await self.session_manager.restore_to_agent(agent, session_data)
            self._bind_session_id_to_context(agent)
            self._init_session_trajectory(agent, overwrite=False)
            return agent

        # 尝试从持久化恢复
        if session_data:
            logger.info(f"从持久化恢复 Agent: {self.session_id}")
            agent = await self.agent_factory(self.agent_name, self.workspace_path)
            await self.session_manager.persistence.restore_to_agent(agent, session_data)
        else:
            logger.info(f"创建新 Agent: {self.session_id}")
            agent = await self.agent_factory(self.agent_name, self.workspace_path)

        self._bind_session_id_to_context(agent)

        # 缓存
        self.session_manager.cache_agent(self.session_id, agent, self.agent_name, "gpt-4")

        self._init_session_trajectory(agent, overwrite=False)

        return agent

    async def _inject_heartbeat_context(
        self,
        agent: Any,
        heartbeat_content: str,
        *,
        mode: str = "execute_due",
        current_task: Any = None,
    ) -> str:
        """Inject heartbeat context according to execution mode."""
        try:
            context = agent.executor.context
            self._bind_session_id_to_context(agent)

            parse_result = self._last_parse_result
            context.set_variable("heartbeat_mode", mode)
            context.set_variable("heartbeat_time", datetime.now().isoformat())
            context.set_variable("heartbeat_corruption_detected", mode == "corrupted")

            header = f"[系统心跳 - {datetime.now().strftime('%Y-%m-%d %H:%M')}]"
            if mode == "corrupted":
                parse_error = ""
                raw_json = ""
                if parse_result and parse_result.status == ParseStatus.CORRUPTED:
                    parse_error = parse_result.parse_error or ""
                    raw_json = parse_result.raw_json_content or ""
                snapshot_summary = self._render_snapshot_summary()
                return f"""
{header}

⚠️ HEARTBEAT.md 的 JSON 任务块解析失败。

错误信息:
{parse_error or "(unknown parse error)"}

原始 JSON 内容:
```json
{raw_json}
```

最近快照摘要:
{snapshot_summary}

请先诊断并修复 HEARTBEAT.md 的任务结构。修复后再继续常规心跳任务。
如果确认无需修复，回复 "HEARTBEAT_OK"。
"""

            if mode == "reflect":
                return f"""
{header}

当前没有到期任务，请进入 routine 反思阶段：
1. 检查 MEMORY.md 和近期对话中的周期性意图
2. 识别尚未注册的 routine
3. 若发现可注册 routine，使用可用工具进行注册
4. 若无可执行动作，回复 "HEARTBEAT_OK"

当前 HEARTBEAT.md 内容：
{heartbeat_content}
"""

            if current_task is not None:
                task_detail = (
                    f"## 当前执行任务\n\n"
                    f"- **ID**: {current_task.id}\n"
                    f"- **标题**: {current_task.title}\n"
                )
                if current_task.description:
                    task_detail += f"- **说明**: {current_task.description}\n"
                return f"""
{header}

{task_detail}
---

请执行上述任务并汇报结果。
"""

            return f"{header}\nHEARTBEAT_OK"
        except Exception as e:
            logger.error("Failed to inject heartbeat context: %s", e)
            raise

    async def _run_agent(
        self,
        agent: Any,
        message: str,
        *,
        system_prompt_override: Optional[str] = None,
    ) -> str:
        """执行 Agent"""
        if isinstance(system_prompt_override, str):
            return await self._run_agent_with_override(agent, message, system_prompt_override)
        return await self._run_heartbeat_turn(agent, message)

    @staticmethod
    def _extract_llm_result(events: list[dict[str, Any]]) -> str:
        """Extract final LLM text from streamed turn events."""
        answer = ""
        deltas: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            progress_list = event.get("_progress")
            if not isinstance(progress_list, list):
                continue
            for progress in progress_list:
                if not isinstance(progress, dict):
                    continue
                if progress.get("stage") != "llm":
                    continue
                part = progress.get("delta")
                if isinstance(part, str) and part:
                    deltas.append(part)
                full = progress.get("answer")
                if isinstance(full, str) and full:
                    answer = full
        if answer:
            return answer
        return "".join(deltas)

    async def _load_or_create_turn_session(self, session_id: str, session_type: str) -> Any:
        """Load one session for TurnExecutor or build a lightweight stub."""
        load_session = getattr(self.session_manager, "load_session", None)
        if callable(load_session):
            loaded = await load_session(session_id)
            if loaded is not None:
                return loaded
        return SimpleNamespace(
            session_id=session_id,
            agent_name=self.agent_name,
            session_type=session_type,
            mailbox=[],
            variables={},
        )

    async def _save_turn_session(self, session_id: str, agent: Any) -> None:
        """Persist turn session while heartbeat lock is already held."""
        save_session = getattr(self.session_manager, "save_session", None)
        if not callable(save_session):
            return
        try:
            await save_session(session_id, agent, lock_already_held=True)
        except TypeError:
            await save_session(session_id, agent)

    async def _run_heartbeat_turn(self, agent: Any, message: str) -> str:
        """Run heartbeat turn via TurnExecutor and runtime context strategies."""
        try:
            ctx = agent.executor.context
            self._runtime_workspace_instructions = self._get_workspace_instructions(ctx)
            ensure_continue_chat_compatibility()

            from .events import emit
            _emit_kw = dict(agent_name=self.agent_name, scope=self.broadcast_scope,
                            source_type="heartbeat", run_id=getattr(self, '_current_run_id', None))
            await emit(self.primary_session_id, {"type": "status", "content": "后台心跳检查中..."}, **_emit_kw)
            try:
                async def _load_session(_session_id: str) -> Any:
                    return await self._load_or_create_turn_session(_session_id, "heartbeat")

                async def _reuse_agent(_session: Any) -> Any:
                    return agent

                turn_result = await self._turn_executor.execute_turn(
                    session_id=self.session_id,
                    trigger=message,
                    load_session=_load_session,
                    get_or_create_agent=_reuse_agent,
                    save_session=self._save_turn_session,
                    stream_mode="delta",
                )
            finally:
                await emit(self.primary_session_id, {"type": "status", "content": ""}, **_emit_kw)

            return self._extract_llm_result(turn_result.events)
        except Exception as e:
            logger.error(
                "Heartbeat agent execution failed: type=%s detail=%s",
                type(e).__name__,
                str(e),
            )
            logger.error(f"执行 Agent 失败: {e}")
            raise

    async def _run_agent_with_override(
        self,
        agent: Any,
        message: str,
        system_prompt_override: str,
    ) -> str:
        """Run direct continue_chat path for explicit system-prompt override cases."""
        try:
            result = ""
            ensure_continue_chat_compatibility()

            from .events import emit
            _emit_kw = dict(agent_name=self.agent_name, scope=self.broadcast_scope,
                            source_type="heartbeat", run_id=getattr(self, '_current_run_id', None))
            await emit(self.primary_session_id, {"type": "status", "content": "后台心跳检查中..."}, **_emit_kw)
            try:
                async for event in agent.continue_chat(
                    message=message,
                    stream_mode="delta",
                    system_prompt=system_prompt_override,
                ):
                    if "_progress" in event:
                        for progress in event["_progress"]:
                            if progress.get("stage") == "llm":
                                answer = progress.get("answer", "")
                                if answer:
                                    result = answer
            finally:
                await emit(self.primary_session_id, {"type": "status", "content": ""}, **_emit_kw)
            return result
        except Exception as e:
            logger.error(
                "Heartbeat agent execution failed: type=%s detail=%s",
                type(e).__name__,
                str(e),
            )
            logger.error(f"执行 Agent 失败: {e}")
            raise

    async def run_once(self):
        """执行一次心跳（带前置检查）"""
        return await self.run_once_with_options(force=False)

    async def run_once_with_options(
        self,
        *,
        force: bool = False,
        include_inline: bool = True,
        include_isolated: bool = True,
    ) -> str:
        """
        Run a single heartbeat.

        Args:
            force: If True, ignore active-hours gating.
            include_inline: If False, skip inline tasks in heartbeat turn.
            include_isolated: If False, skip isolated tasks in heartbeat turn.
        """
        if (not force) and (not self._is_active_time()):
            logger.debug(f"[{self.agent_name}] 非活跃时段，跳过")
            self._write_heartbeat_event("skipped", reason="inactive")
            return "HEARTBEAT_SKIPPED_INACTIVE"

        logger.info(f"[{self.agent_name}] 开始心跳")

        try:
            result = await self._execute_with_retry(
                include_inline=include_inline,
                include_isolated=include_isolated,
            )

            if self._should_skip_response(result):
                logger.debug(f"[{self.agent_name}] 静默响应")
            else:
                self._last_result = result
                logger.info(f"[{self.agent_name}] 心跳结果: {result[:100]}...")

                if self.on_result:
                    await self._emit_result(result)
            return result

        except Exception as e:
            failure_summary = f"HEARTBEAT_FAILED: {type(e).__name__}: {e}"
            logger.error("Heartbeat run failed: %s", failure_summary)
            if self.on_result:
                try:
                    await self._emit_result(failure_summary)
                except Exception as callback_error:
                    logger.error("Heartbeat failure callback failed: %s", callback_error)
            logger.error(f"[{self.agent_name}] 心跳失败: {e}")
            return "HEARTBEAT_FAILED"

    async def start(self):
        """启动心跳循环"""
        self._running = True
        logger.info(f"[{self.agent_name}] 心跳启动，间隔 {self.interval_minutes} 分钟")

        while self._running:
            await self.run_once()
            await asyncio.sleep(self.interval_minutes * 60)

    def stop(self):
        """停止心跳"""
        self._running = False
        logger.info(f"[{self.agent_name}] 心跳已停止")
