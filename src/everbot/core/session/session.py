"""
Session 管理
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import logging

from .compressor import SessionCompressor
from . import session_ids as _sid
from . import session_mailbox as _mailbox

logger = logging.getLogger(__name__)


# Re-export SessionData for backward compatibility
from .session_data import SessionData


# Re-export SessionPersistence for backward compatibility
from .persistence import SessionPersistence


class SessionManager:
    """
    Session 管理器

    带并发控制的 Session 管理。
    """

    MAX_TIMELINE_EVENTS = 500  # 防止内存泄漏，限制每个 session 的 timeline 事件数
    _MAX_CACHED_LOCKS = 200
    MEMORY_EXTRACTION_TIMEOUT = 120  # seconds; LLM-based memory extraction after session save

    # --- Delegated ID helpers (canonical implementations in session_ids.py) ---

    @staticmethod
    def get_primary_session_id(agent_name: str) -> str:
        """Return the canonical long-lived session id for one agent."""
        return _sid.get_primary_session_id(agent_name)

    @staticmethod
    def infer_session_type(session_id: str) -> str:
        """Infer runtime session type from session id."""
        return _sid.infer_session_type(session_id)

    @staticmethod
    def get_heartbeat_session_id(agent_name: str) -> str:
        """Return heartbeat-only session id for one agent."""
        return _sid.get_heartbeat_session_id(agent_name)

    @staticmethod
    def get_session_prefix(agent_name: str) -> str:
        """Return the session id prefix for one agent."""
        return _sid.get_session_prefix(agent_name)

    @staticmethod
    def resolve_agent_name(session_id: str) -> Optional[str]:
        """Extract agent name from a session ID."""
        return _sid.resolve_agent_name(session_id)

    @classmethod
    def is_valid_agent_session_id(cls, agent_name: str, session_id: str) -> bool:
        """Validate one session id belongs to the given agent namespace."""
        return _sid.is_valid_agent_session_id(agent_name, session_id)

    @classmethod
    def create_chat_session_id(cls, agent_name: str) -> str:
        """Create a new chat session id for one agent."""
        return _sid.create_chat_session_id(agent_name)

    def __init__(self, sessions_dir: Path):
        self.persistence = SessionPersistence(sessions_dir)
        self._agents: Dict[str, Any] = {}  # session_id -> DolphinAgent
        self._locks: Dict[str, tuple] = {}  # session_id -> (event_loop, Lock)
        self._agent_metadata: Dict[str, Dict[str, str]] = {}  # session_id -> metadata
        self._timeline_events: Dict[str, list] = {}  # session_id -> timeline events
        self._timeline_lock = threading.Lock()  # 保护 timeline 操作的线程锁
        self._metrics: Dict[str, float] = {}
        self._metrics_lock = threading.Lock()

    def get_session_summary(
        self, session_id: str, max_chars: int = 500
    ) -> Optional[str]:
        """Return a plain-text summary of recent conversation in a session.

        Extracts the last few user/assistant messages from persisted history
        and concatenates them into a compact string for Inspector reflection.
        Synchronous — reads the session file directly to avoid async overhead
        since this is called from sync ``_gather_context``.
        """
        session_path = self.persistence._get_session_path(session_id)
        if not session_path.exists():
            return None

        try:
            raw = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        history = raw.get("history_messages") or []
        if not history:
            return None

        # Collect recent user/assistant messages (skip system/tool)
        relevant: List[str] = []
        for msg in reversed(history):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            label = "U" if role == "user" else "A"
            relevant.append(f"[{label}] {content.strip()}")
            if len(relevant) >= 6:
                break

        if not relevant:
            return None

        # Reverse back to chronological order and truncate
        relevant.reverse()
        summary = "\n".join(relevant)
        if len(summary) > max_chars:
            summary = summary[-max_chars:]
        return summary

    def get_last_activity_time(self, agent_name: str) -> Optional[float]:
        """Return Unix timestamp of the latest user activity across agent chat sessions.

        Aggregates persisted ``updated_at`` values across primary and channel
        sessions for the given agent. Heartbeat/job sessions are excluded
        because they represent background execution, not user activity.
        Returns ``None`` when no qualifying session exists or all timestamps
        are unreadable.
        """
        from ..channel.session_resolver import ChannelSessionResolver

        patterns = [
            f"{self.get_primary_session_id(agent_name)}.json",
            f"{self.get_session_prefix(agent_name)}*.json",
        ]
        for channel_type, prefix in ChannelSessionResolver._PREFIX_MAP.items():
            if channel_type == "web":
                continue
            patterns.append(f"{prefix}{agent_name}{ChannelSessionResolver._SEP}*.json")

        latest: Optional[float] = None
        seen_paths: set[Path] = set()
        for pattern in patterns:
            for session_path in self.persistence.sessions_dir.glob(pattern):
                if session_path in seen_paths or not session_path.is_file():
                    continue
                seen_paths.add(session_path)
                try:
                    raw = json.loads(session_path.read_text(encoding="utf-8"))
                except Exception:
                    logger.debug("Failed to read session file for activity scan: %s", session_path, exc_info=True)
                    continue

                session_type = raw.get("session_type") or self.infer_session_type(raw.get("session_id") or session_path.stem)
                if session_type not in {"primary", "channel", "sub"}:
                    continue
                if raw.get("agent_name") not in {None, "", agent_name}:
                    continue

                ua = raw.get("updated_at")
                if not ua:
                    continue
                try:
                    ts = datetime.fromisoformat(ua).timestamp()
                except (ValueError, TypeError):
                    continue
                if latest is None or ts > latest:
                    latest = ts
        return latest

    def record_metric(self, name: str, delta: float = 1.0) -> None:
        """Increment one runtime metric counter."""
        key = str(name or "").strip()
        if not key:
            return
        with self._metrics_lock:
            current = float(self._metrics.get(key, 0.0))
            self._metrics[key] = current + float(delta)

    def observe_metric_ms(self, name: str, value_ms: float) -> None:
        """Record one latency observation and keep an average under *name*."""
        key = str(name or "").strip()
        if not key:
            return
        total_key = f"{key}__total"
        count_key = f"{key}__count"
        with self._metrics_lock:
            total = float(self._metrics.get(total_key, 0.0)) + float(value_ms)
            count = float(self._metrics.get(count_key, 0.0)) + 1.0
            self._metrics[total_key] = total
            self._metrics[count_key] = count
            self._metrics[key] = total / count if count > 0 else 0.0

    def get_metrics_snapshot(self) -> Dict[str, float]:
        """Return a copy of runtime metrics for observability."""
        with self._metrics_lock:
            return dict(self._metrics)

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        """Parse ISO datetime and normalize to UTC-aware datetime."""
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _is_mailbox_event_stale(
        self,
        event: Dict[str, Any],
        *,
        now_utc: datetime,
        stale_after: timedelta = timedelta(hours=24),
    ) -> bool:
        return _mailbox.is_mailbox_event_stale(event, now_utc=now_utc, stale_after=stale_after)

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """获取 Session 锁（懒创建，带 LRU 淘汰，event loop 安全）"""
        current_loop = asyncio.get_running_loop()
        cached = self._locks.get(session_id)
        if cached is not None:
            cached_loop, lock = cached
            if cached_loop is current_loop:
                return lock
            # Event loop changed — discard stale lock
            logger.debug("Discarding stale lock for %s (event loop changed)", session_id)

        if len(self._locks) >= self._MAX_CACHED_LOCKS:
            to_remove = [k for k, (_, v) in self._locks.items() if not v.locked()]
            for k in to_remove[:len(to_remove) // 2]:
                del self._locks[k]
        new_lock = asyncio.Lock()
        self._locks[session_id] = (current_loop, new_lock)
        return new_lock

    async def acquire_session(self, session_id: str, timeout: float = 30.0) -> bool:
        """
        获取 Session 锁

        Args:
            session_id: 会话 ID
            timeout: 超时时间（秒）

        Returns:
            是否成功获取锁
        """
        lock = self._get_lock(session_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("获取 Session 锁超时: %s", session_id)
            return False

    def release_session(self, session_id: str):
        """释放 Session 锁"""
        lock = self._get_lock(session_id)
        if lock.locked():
            lock.release()

    @asynccontextmanager
    async def session_context(self, session_id: str, timeout: float = 30.0):
        """
        Session 上下文管理器

        Usage:
            async with session_manager.session_context("session_123") as acquired:
                if acquired:
                    # 执行操作
                else:
                    # 处理锁获取失败
        """
        acquired = await self.acquire_session(session_id, timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release_session(session_id)

    def cache_agent(self, session_id: str, agent: Any, agent_name: str, model_name: str):
        """缓存 Agent 实例"""
        self._agents[session_id] = agent
        self._agent_metadata[session_id] = {
            "agent_name": agent_name,
            "model_name": model_name,
        }

    def get_cached_agent(self, session_id: str) -> Optional[Any]:
        """从缓存获取 Agent"""
        return self._agents.get(session_id)

    @staticmethod
    def _extract_context_trace(agent: Any) -> Dict[str, Any]:
        """Extract a JSON-serializable context trace from agent if available.

        When the trace reports estimated_output_tokens=0 but the agent actually
        produced visible output (tool calls and/or text responses), we estimate
        token count from history to avoid under-reporting.
        """
        if agent is None or not hasattr(agent, "get_execution_trace"):
            return {}
        try:
            raw_trace = agent.get_execution_trace()
            if isinstance(raw_trace, str):
                parsed = json.loads(raw_trace)
                trace = parsed if isinstance(parsed, dict) else {}
            elif isinstance(raw_trace, dict):
                trace = raw_trace
            else:
                return {}
        except Exception:
            return {}

        # --- Fix under-reported output tokens ---
        # When the first LLM turn is thinking-only (0 visible output tokens)
        # but subsequent turns produce tool calls + text, the trace snapshot
        # only captures turn-1's count.  Estimate from history instead.
        if trace.get("estimated_output_tokens", -1) == 0:
            try:
                portable = agent.snapshot.export_portable_session()
                history = portable.get("history_messages", [])
                estimated = 0
                for msg in history:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content", "")
                    if content:
                        estimated += len(content) // 2  # rough char-to-token
                    if msg.get("tool_calls"):
                        estimated += len(msg["tool_calls"]) * 20  # per tool call overhead
                if estimated > 0:
                    trace["estimated_output_tokens"] = estimated
            except Exception:
                pass

        return trace

    async def update_atomic(
        self,
        session_id: str,
        mutator: Callable[[SessionData], None],
        *,
        timeout: float = 10.0,
        blocking: bool = True,
        bump_updated_at: bool = True,
    ) -> Optional[SessionData]:
        """Atomic read-modify-write with dual-layer locking.

        Layer 1: asyncio.Lock (in-process, reduces contention among coroutines).
        Layer 2: fcntl.flock (cross-process, protects daemon vs web).

        Returns updated SessionData on success, None if lock not acquired.
        """
        lock = self._get_lock(session_id)
        wait_started = time.perf_counter()
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            self.observe_metric_ms("lock_wait_ms", (time.perf_counter() - wait_started) * 1000.0)
            self.record_metric("lock_timeout_count")
            if not blocking:
                return None
            logger.warning("In-process lock timeout for %s", session_id)
            return None
        self.observe_metric_ms("lock_wait_ms", (time.perf_counter() - wait_started) * 1000.0)
        try:
            return await self.persistence.update_atomic(
                session_id, mutator, timeout=timeout, blocking=blocking,
                bump_updated_at=bump_updated_at,
            )
        finally:
            lock.release()

    async def deposit_mailbox_event(
        self, session_id: str, event: Dict[str, Any],
        *, timeout: float = 5.0, blocking: bool = True,
    ) -> bool:
        """Append one event into session mailbox atomically with idempotency."""
        return await _mailbox.deposit_mailbox_event(
            self, session_id, event, timeout=timeout, blocking=blocking,
        )

    async def inject_history_message(
        self, session_id: str, message: dict,
        *, timeout: float = 5.0, blocking: bool = True,
    ) -> bool:
        """Append one message into session history_messages atomically."""
        return await _mailbox.inject_history_message(
            self, session_id, message, timeout=timeout, blocking=blocking,
        )

    async def ack_mailbox_events(
        self, session_id: str, event_ids: list[str],
        *, timeout: float = 5.0, blocking: bool = True,
        lock_already_held: bool = False,
    ) -> bool:
        """Remove consumed mailbox events by event_id atomically."""
        return await _mailbox.ack_mailbox_events(
            self, session_id, event_ids, timeout=timeout, blocking=blocking,
            lock_already_held=lock_already_held,
        )

    def file_lock(self, session_id: str, **kwargs):
        """Expose file-level lock for callers that need longer lock spans."""
        return self.persistence.file_lock(session_id, **kwargs)

    async def mark_session_archived(
        self,
        session_id: str,
        *,
        timeout: float = 5.0,
        blocking: bool = True,
    ) -> bool:
        """Mark one session as archived."""
        archived_at = datetime.now(timezone.utc).isoformat()

        def _mutator(session_data: SessionData) -> None:
            session_data.state = "archived"
            session_data.archived_at = archived_at

        updated = await self.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking)
        if updated is not None:
            self.record_metric("session_archived_count")
        return updated is not None

    async def cleanup_archived_job_sessions(
        self,
        *,
        retention_days: int = 7,
        max_sessions: int = 200,
    ) -> int:
        """Cleanup archived job sessions by age and cardinality."""
        now_utc = datetime.now(timezone.utc)
        retention_days = max(0, int(retention_days))
        max_sessions = max(0, int(max_sessions))

        archived_jobs: list[tuple[str, datetime]] = []
        for session_file in sorted(self.persistence.sessions_dir.glob("job_*.json")):
            session_id = session_file.stem
            session_data = await self.load_session(session_id)
            if session_data is None:
                continue
            if session_data.session_type != "job":
                continue
            if str(session_data.state or "active") != "archived":
                continue
            archived_ts = self._parse_iso_datetime(session_data.archived_at)
            if archived_ts is None:
                archived_ts = self._parse_iso_datetime(session_data.updated_at) or now_utc
            archived_jobs.append((session_id, archived_ts))

        if not archived_jobs:
            return 0

        to_delete: set[str] = set()
        if retention_days >= 0:
            max_age = timedelta(days=retention_days)
            for session_id, archived_ts in archived_jobs:
                if (now_utc - archived_ts) > max_age:
                    to_delete.add(session_id)

        remaining = [(sid, ts) for sid, ts in archived_jobs if sid not in to_delete]
        remaining.sort(key=lambda item: item[1], reverse=True)
        if max_sessions >= 0 and len(remaining) > max_sessions:
            for sid, _ in remaining[max_sessions:]:
                to_delete.add(sid)

        removed = 0
        for session_id in sorted(to_delete):
            self._agents.pop(session_id, None)
            self._agent_metadata.pop(session_id, None)
            self._timeline_events.pop(session_id, None)
            await self.persistence.delete(session_id)
            removed += 1

        if removed:
            self.record_metric("job_session_cleanup_count", float(removed))
        return removed

    async def save_session(
        self,
        session_id: str,
        agent: Any,
        model_name: str = "gpt-4",
        *,
        lock_already_held: bool = False,
        trailing_messages: Optional[List[Dict[str, Any]]] = None,
    ):
        """保存 Session.

        默认走 ``update_atomic`` 统一写入口。
        在调用方已持有 session 的进程内锁与文件锁时，可设置
        ``lock_already_held=True`` 避免重入锁导致死锁。
        """
        logger.debug("Persisting session %s to disk", session_id)
        timeline = self.get_timeline(session_id)
        context_trace = self._extract_context_trace(agent)

        # Extract structured memories for primary / channel sessions.
        # Fire-and-forget with timeout so it never blocks session persistence.
        session_type = SessionManager.infer_session_type(session_id)
        if session_type in ("primary", "channel"):
            try:
                agent_name = getattr(agent, "name", "")
                if agent_name:
                    context = agent.executor.context
                    from ..memory.manager import MemoryManager
                    from ...infra.user_data import get_user_data_manager
                    memory_path = get_user_data_manager().get_agent_dir(agent_name) / "MEMORY.md"
                    mm = MemoryManager(memory_path, context)
                    portable = agent.snapshot.export_portable_session()
                    history = portable.get("history_messages", [])
                    await asyncio.wait_for(
                        mm.process_session_end(history, session_id),
                        timeout=self.MEMORY_EXTRACTION_TIMEOUT,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "Memory extraction timed out (%ds); skipping",
                    self.MEMORY_EXTRACTION_TIMEOUT,
                )
            except Exception:
                logger.warning("Memory extraction failed; skipping", exc_info=True)

        if lock_already_held:
            await self.persistence.save(
                session_id,
                agent,
                model_name,
                timeline=timeline,
                context_trace=context_trace,
                trailing_messages=trailing_messages,
            )
            logger.debug("Session persisted.")
            return

        context = agent.executor.context
        portable = agent.snapshot.export_portable_session()
        serializable_history = portable.get("history_messages", [])
        exported_variables = portable.get("variables", {})
        exported_variables.pop("_history", None)  # avoid duplicating history_messages
        created_at_hint = context.get_var_value("session_created_at")

        # Compress history for long-lived sessions before entering the lock.
        if SessionManager.infer_session_type(session_id) in ("primary", "channel"):
            try:
                compressor = SessionCompressor(context)
                serializable_history = await compressor.compress_history(serializable_history)
            except Exception:
                logger.warning("History compression failed; saving uncompressed", exc_info=True)

        # Append trailing messages (e.g. failed turn context) to history
        if trailing_messages:
            serializable_history = list(serializable_history) + list(trailing_messages)

        # Strip bare empty assistant messages (content="" with no tool_calls).
        # These are artifacts from failed/timed-out tool executions and cause
        # API errors on providers like DeepSeek.
        serializable_history = self.persistence._filter_empty_assistant_messages(
            serializable_history
        )

        def _mutator(session_data: SessionData) -> None:
            session_data.session_id = session_id
            session_data.agent_name = getattr(agent, "name", "") or session_data.agent_name
            session_data.model_name = model_name
            session_data.session_type = SessionManager.infer_session_type(session_id)
            if not isinstance(session_data.state, str) or not session_data.state:
                session_data.state = "active"
            session_data.history_messages = serializable_history
            if not isinstance(session_data.mailbox, list):
                session_data.mailbox = []
            session_data.variables = exported_variables
            if not session_data.created_at:
                session_data.created_at = created_at_hint or datetime.now(timezone.utc).isoformat()
            session_data.timeline = timeline or []
            session_data.context_trace = context_trace or {}

        updated = await self.update_atomic(session_id, _mutator, timeout=10.0, blocking=True)
        if updated is None:
            raise TimeoutError(f"Failed to persist session {session_id}: lock not acquired")
        logger.debug("Session persisted: %s", session_id)

    async def load_session(self, session_id: str) -> Optional[SessionData]:
        """加载 Session"""
        return await self.persistence.load(session_id)

    async def list_agent_sessions(self, agent_name: str, limit: int = 20) -> list[Dict[str, Any]]:
        """List persisted sessions for one agent ordered by updated time descending."""
        from ..channel.session_resolver import ChannelSessionResolver

        # Collect session files from all channel prefixes
        prefixes = [self.get_session_prefix(agent_name)]
        for channel_type, prefix in ChannelSessionResolver._PREFIX_MAP.items():
            if channel_type == "web":
                continue
            prefixes.append(f"{prefix}{agent_name}")
        seen_files: set = set()
        items: list[Dict[str, Any]] = []
        for pfx in prefixes:
            for session_file in sorted(self.persistence.sessions_dir.glob(f"{pfx}*.json")):
                if session_file in seen_files:
                    continue
                seen_files.add(session_file)
                session_id = session_file.stem
                session_data = await self.load_session(session_id)
                if session_data is None:
                    continue
                items.append(
                    {
                        "session_id": session_data.session_id or session_id,
                        "agent_name": session_data.agent_name or agent_name,
                        "created_at": session_data.created_at,
                        "updated_at": session_data.updated_at,
                        "state": session_data.state,
                        "message_count": len(session_data.history_messages or []),
                        "timeline_count": len(session_data.timeline or []),
                    }
                )
        items.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        if limit > 0:
            items = items[:limit]
        return items

    async def migrate_legacy_sessions_for_agent(self, agent_name: str) -> bool:
        """
        Migrate legacy sessions into the canonical session id.

        Legacy sources:
            - heartbeat_<agent_name>
            - agent_session_<agent_name>
        """
        target_session_id = self.get_primary_session_id(agent_name)
        legacy_session_ids = [
            f"heartbeat_{agent_name}",
            f"agent_session_{agent_name}",
        ]

        target = await self.load_session(target_session_id)
        migrated_any = False

        for legacy_id in legacy_session_ids:
            if legacy_id == target_session_id:
                continue

            legacy = await self.load_session(legacy_id)
            if legacy is None:
                continue

            migrated_any = True
            if target is None:
                target = legacy
                target.session_id = target_session_id
                target.updated_at = datetime.now(timezone.utc).isoformat()
                if not isinstance(target.variables, dict):
                    target.variables = {}
                target.variables.setdefault("_migrated_from", [])
                target.variables["_migrated_from"].append(legacy_id)
            else:
                if not target.history_messages and legacy.history_messages:
                    target.history_messages = legacy.history_messages
                if not target.context_trace and legacy.context_trace:
                    target.context_trace = legacy.context_trace

                merged_timeline = (target.timeline or []) + (legacy.timeline or [])
                merged_timeline.sort(key=lambda x: str(x.get("timestamp", "")))
                target.timeline = merged_timeline

                if not isinstance(target.variables, dict):
                    target.variables = {}
                target.variables.setdefault("_migrated_from", [])
                target.variables["_migrated_from"].append(legacy_id)
                target.updated_at = datetime.now(timezone.utc).isoformat()

            legacy_path = self.persistence._get_session_path(legacy_id)
            if legacy_path.exists():
                migrated_backup = legacy_path.with_suffix(
                    legacy_path.suffix + f".migrated_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                )
                try:
                    legacy_path.rename(migrated_backup)
                except Exception:
                    logger.warning("Legacy session backup failed: %s", legacy_path)

            self._agents.pop(legacy_id, None)
            self._agent_metadata.pop(legacy_id, None)
            self._timeline_events.pop(legacy_id, None)

        if migrated_any and target is not None:
            await self.persistence.save_data(target)
            logger.info("Session migration completed for agent=%s target=%s", agent_name, target_session_id)

        return migrated_any

    async def clear_session_history(self, session_id: str) -> bool:
        """清除 Session 的对话历史，保留 session 元数据（session_id, agent_name 等）。

        Returns True if session existed and was cleared, False otherwise.
        """
        session_data = await self.persistence.load(session_id)
        if session_data is None:
            return False
        session_data.history_messages = []
        session_data.events = []
        session_data.timeline = []
        session_data.context_trace = {}
        session_data.updated_at = datetime.now(timezone.utc).isoformat()
        await self.persistence.save_data(session_data)
        # Also clear in-memory caches so next load is fresh
        self._agents.pop(session_id, None)
        self._agent_metadata.pop(session_id, None)
        self._timeline_events.pop(session_id, None)
        # Reset trajectory file so the next turn starts a fresh debug log
        self._reset_trajectory_file(session_data.agent_name, session_id)
        logger.info("Session history cleared: %s", session_id)
        return True

    def _reset_trajectory_file(self, agent_name: str, session_id: str) -> None:
        """Archive the current trajectory file and start a fresh one."""
        try:
            from ...infra.user_data import get_user_data_manager
            trajectory_path = get_user_data_manager().get_session_trajectory_path(
                agent_name, session_id
            )
            if trajectory_path.exists() and trajectory_path.stat().st_size > 50:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                archive_path = trajectory_path.with_suffix(f".{ts}.json")
                trajectory_path.rename(archive_path)
                logger.debug("Trajectory archived: %s", archive_path)
            trajectory_path.write_text(
                '{"trajectory": [], "tools": [], "stages": []}',
                encoding="utf-8",
            )
            logger.debug("Trajectory file reset: %s", trajectory_path)
        except Exception:
            logger.debug("Failed to reset trajectory file", exc_info=True)

    async def reset_session(self, session_id: str):
        """重置 Session：清除缓存、Dolphin context 和磁盘文件。

        Clears the agent's in-memory Dolphin context (history + variables)
        before removing from cache, so that any stale reference cannot leak
        old conversation history into a new session.
        """
        agent = self._agents.pop(session_id, None)
        if agent is not None:
            self._clear_agent_context(agent)
        self._agent_metadata.pop(session_id, None)
        self._timeline_events.pop(session_id, None)
        await self.persistence.delete(session_id)
        logger.info("Session 已重置: %s", session_id)

    @staticmethod
    def _clear_agent_context(agent: Any) -> None:
        """Clear an agent's Dolphin context to prevent stale history leaks."""
        try:
            ctx = getattr(getattr(agent, "executor", None), "context", None)
            if ctx is None:
                return
            from dolphin.core.common.constants import KEY_HISTORY
            if hasattr(ctx, "set_variable"):
                ctx.set_variable(KEY_HISTORY, [])
            if hasattr(ctx, "_history"):
                ctx._history = []
            if hasattr(ctx, "set_history_bucket"):
                ctx.set_history_bucket([])
        except Exception:
            logger.debug("Failed to clear agent context", exc_info=True)

    async def reset_agent_sessions(self, agent_name: str) -> int:
        """Reset all sessions for one agent and return removed count.

        Covers web sessions (``web_session_``), heartbeat sessions, and
        non-web channel sessions (``tg_session_``, ``discord_session_``, etc.).
        """
        from ..channel.session_resolver import ChannelSessionResolver

        # Collect all session prefixes that belong to this agent.
        prefixes = [self.get_session_prefix(agent_name)]
        for channel_type, prefix in ChannelSessionResolver._PREFIX_MAP.items():
            if channel_type == "web":
                continue
            prefixes.append(f"{prefix}{agent_name}{ChannelSessionResolver._SEP}")
        # Also include heartbeat session
        prefixes.append(f"heartbeat_session_{agent_name}")

        targets: list[str] = []
        for pfx in prefixes:
            targets.extend(
                p.stem for p in self.persistence.sessions_dir.glob(f"{pfx}*.json")
            )

        removed = 0
        for session_id in targets:
            await self.reset_session(session_id)
            removed += 1
        for session_id in list(self._agent_metadata.keys()):
            meta = self._agent_metadata.get(session_id) or {}
            if meta.get("agent_name") == agent_name:
                agent = self._agents.pop(session_id, None)
                if agent is not None:
                    self._clear_agent_context(agent)
                self._agent_metadata.pop(session_id, None)
                self._timeline_events.pop(session_id, None)
        return removed

    async def restore_to_agent(self, agent: Any, session_data: SessionData, **kwargs):
        """恢复 Session 到 Agent"""
        await self.persistence.restore_to_agent(agent, session_data, **kwargs)

    def append_timeline_event(self, session_id: str, event: Dict[str, Any]):
        """
        Append one timeline event in memory for a session.
        
        Event types:
            - turn_start: 用户消息开始处理
            - llm_start: LLM 首 token 到达
            - tool_call: 工具调用开始
            - tool_output: 工具输出返回
            - skill: Skill 执行
            - turn_end: 本轮处理结束
        """
        with self._timeline_lock:
            if session_id not in self._timeline_events:
                self._timeline_events[session_id] = []
            events = self._timeline_events[session_id]
            events.append(dict(event))
            # 防止内存泄漏：超过上限时移除最早的事件
            if len(events) > self.MAX_TIMELINE_EVENTS:
                self._timeline_events[session_id] = events[-self.MAX_TIMELINE_EVENTS:]

    def get_timeline(self, session_id: str) -> list:
        """Get in-memory timeline events for a session."""
        with self._timeline_lock:
            return list(self._timeline_events.get(session_id, []))

    def restore_timeline(self, session_id: str, events: list):
        """Restore timeline events into memory from persisted session data."""
        if not isinstance(events, list):
            return
        with self._timeline_lock:
            self._timeline_events[session_id] = [dict(event) for event in events[-self.MAX_TIMELINE_EVENTS:] if isinstance(event, dict)]

    def clear_timeline(self, session_id: str):
        """Clear in-memory timeline events for a session."""
        with self._timeline_lock:
            self._timeline_events[session_id] = []
