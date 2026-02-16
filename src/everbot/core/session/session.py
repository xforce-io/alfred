"""
Session 管理
"""

import asyncio
import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
import re
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
import logging

from ...infra.dolphin_state_adapter import DolphinStateAdapter

logger = logging.getLogger(__name__)


@dataclass
class SessionData:
    """Session 持久化数据"""
    session_id: str
    agent_name: str
    model_name: str
    session_type: str
    history_messages: list  # List[Dict]
    mailbox: list
    variables: Dict[str, Any]
    created_at: str
    updated_at: str
    state: str = "active"
    archived_at: Optional[str] = None
    events: list = None  # UI events like tool calls
    timeline: list = None
    context_trace: Dict[str, Any] = None
    revision: int = 0

    def __init__(self, **kwargs):
        # Compatibility for old sessions
        self.session_id = kwargs.get("session_id")
        self.agent_name = kwargs.get("agent_name")
        self.model_name = kwargs.get("model_name")
        self.session_type = kwargs.get("session_type") or SessionManager.infer_session_type(self.session_id or "")
        self.history_messages = kwargs.get("history_messages", [])
        self.mailbox = kwargs.get("mailbox", [])
        self.variables = kwargs.get("variables", {})
        self.created_at = kwargs.get("created_at")
        self.updated_at = kwargs.get("updated_at")
        self.state = kwargs.get("state", "active")
        self.archived_at = kwargs.get("archived_at")
        self.events = kwargs.get("events", [])
        self.timeline = kwargs.get("timeline", kwargs.get("trajectory_events", []))
        self.context_trace = kwargs.get("context_trace", {})
        self.revision = kwargs.get("revision", 0)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionData":
        return cls(**data)


class SessionPersistence:
    """Session 持久化管理器"""

    # Keep a larger restoration window to reduce truncation-related context loss.
    MAX_RESTORED_HISTORY_MESSAGES = 120
    SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # ── Atomic write helpers ──────────────────────────────────────

    @staticmethod
    def _compute_checksum(data: bytes) -> str:
        """Compute SHA-256 hex digest for integrity verification."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def atomic_save(path: Path, data: bytes) -> None:
        """Write *data* to *path* atomically with .bak rotation.

        Steps:
        1. Write to .tmp + fsync
        2. Rotate existing file → .bak
        3. os.replace .tmp → target (atomic on POSIX)
        4. fsync directory (best-effort)
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        bak = path.with_suffix(path.suffix + ".bak")

        # 1) write tmp + fsync
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        # 2) rotate old → bak
        if path.exists():
            os.replace(path, bak)

        # 3) atomic rename
        os.replace(tmp, path)

        # 4) fsync directory (best-effort, POSIX only)
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

    def _serialize_session(self, data_dict: Dict) -> bytes:
        """Serialize session dict to JSON bytes with embedded checksum.

        The checksum is always computed on the payload **without** the
        ``_checksum`` key so that validation is independent of JSON key
        ordering.  We insert the checksum into the dict only once and
        serialize exactly once to guarantee consistency.
        """
        # Remove any stale checksum before computing new one
        data_dict.pop("_checksum", None)
        payload_bytes = json.dumps(data_dict, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        checksum = self._compute_checksum(payload_bytes)
        # Insert checksum and produce final serialization
        data_dict["_checksum"] = checksum
        final = json.dumps(data_dict, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        return final

    def _validate_and_load_json(self, raw: bytes) -> Optional[Dict]:
        """Parse JSON bytes and verify checksum.  Returns dict or None."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        stored_checksum = data.pop("_checksum", None)
        if stored_checksum is not None:
            # Re-serialize without checksum using deterministic key order
            payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            if self._compute_checksum(payload) != stored_checksum:
                logger.warning("Session checksum mismatch — treating as corrupt")
                return None
        return data

    @classmethod
    def is_safe_session_id(cls, session_id: str) -> bool:
        """Validate session id to prevent path traversal and invalid filenames."""
        if not session_id or not isinstance(session_id, str):
            return False
        return bool(cls.SESSION_ID_PATTERN.fullmatch(session_id))

    def _get_session_path(self, session_id: str) -> Path:
        """获取 Session 文件路径"""
        if not self.is_safe_session_id(session_id):
            raise ValueError(f"Invalid session_id: {session_id!r}")
        return self.sessions_dir / f"{session_id}.json"

    async def save(
        self,
        session_id: str,
        agent: Any,
        model_name: str = "gpt-4",
        timeline: Optional[list] = None,
        context_trace: Optional[Dict[str, Any]] = None,
    ):
        """
        保存 Session 到文件

        Dolphin 的 _history 变量已包含完整的 tool chain（assistant tool_calls +
        tool responses），直接序列化即可，无需额外的 events sidecar。

        Args:
            session_id: 会话 ID
            agent: DolphinAgent 实例
            model_name: 模型名称
        """
        try:
            context = agent.executor.context
            exported_state = DolphinStateAdapter.export_session_state(agent)
            serializable_history = exported_state.get("history_messages", [])
            previous = await self.load(session_id)
            next_revision = ((previous.revision if previous else 0) or 0) + 1
            created_at = (
                context.get_var_value("session_created_at")
                or (previous.created_at if previous and previous.created_at else None)
                or datetime.now().isoformat()
            )

            data = SessionData(
                session_id=session_id,
                agent_name=agent.name,
                model_name=model_name,
                session_type=SessionManager.infer_session_type(session_id),
                history_messages=serializable_history,
                mailbox=(previous.mailbox if previous and isinstance(previous.mailbox, list) else []),
                variables={
                    "workspace_instructions": context.get_var_value("workspace_instructions"),
                    "model_name": context.get_var_value("model_name"),
                    "current_time": context.get_var_value("current_time"),
                },
                created_at=created_at,
                updated_at=datetime.now().isoformat(),
                state=(previous.state if previous and isinstance(previous.state, str) else "active"),
                archived_at=(previous.archived_at if previous else None),
                timeline=timeline or [],
                context_trace=context_trace or {},
                revision=next_revision,
            )

            session_path = self._get_session_path(session_id)
            serialized = self._serialize_session(data.to_dict())
            self.atomic_save(session_path, serialized)

            logger.debug(f"Session 已保存: {session_id}")

        except Exception as e:
            logger.error(f"保存 Session 失败: {e}")
            raise

    def _postprocess_loaded_data(self, data: Dict) -> SessionData:
        """Apply backward-compat fixups and return SessionData."""
        # 清理旧格式残留：移除 messages 内嵌的 events sidecar（仅占空间）
        for msg in data.get("history_messages", []):
            if isinstance(msg, dict):
                msg.pop("events", None)

        data["history_messages"] = data.get("history_messages", [])
        data["events"] = []
        if "timeline" not in data or not isinstance(data.get("timeline"), list):
            legacy_timeline = data.get("trajectory_events", [])
            data["timeline"] = legacy_timeline if isinstance(legacy_timeline, list) else []
        if "context_trace" not in data or not isinstance(data.get("context_trace"), dict):
            data["context_trace"] = {}
        if "mailbox" not in data or not isinstance(data.get("mailbox"), list):
            data["mailbox"] = []
        if "session_type" not in data or not isinstance(data.get("session_type"), str):
            data["session_type"] = SessionManager.infer_session_type(str(data.get("session_id") or ""))
        if "state" not in data or not isinstance(data.get("state"), str):
            data["state"] = "active"
        if "archived_at" not in data:
            data["archived_at"] = None
        return SessionData.from_dict(data)

    async def load(self, session_id: str) -> Optional[SessionData]:
        """
        从文件加载 Session

        新格式：history_messages 直接包含 tool chain 消息（role=tool 等），
        无需单独的 events 字段。旧格式文件中嵌入的 events 字段会被忽略。

        Corruption recovery: if the main file is invalid (parse error or
        checksum mismatch), fall back to the .bak file.
        """
        session_path = self._get_session_path(session_id)
        bak_path = session_path.with_suffix(session_path.suffix + ".bak")

        # Try main file first
        if session_path.exists():
            try:
                raw = session_path.read_bytes()
                data = self._validate_and_load_json(raw)
                if data is not None:
                    return self._postprocess_loaded_data(data)
                logger.warning("Main session file corrupt for %s, trying .bak", session_id)
            except Exception as e:
                logger.warning("Failed to read main session file for %s: %s", session_id, e)

        # Fallback to .bak
        if bak_path.exists():
            try:
                raw = bak_path.read_bytes()
                data = self._validate_and_load_json(raw)
                if data is not None:
                    logger.info("Recovered session %s from .bak file", session_id)
                    return self._postprocess_loaded_data(data)
                logger.warning("Backup file also corrupt for %s", session_id)
            except Exception as e:
                logger.warning("Failed to read .bak session file for %s: %s", session_id, e)

        if session_path.exists() or bak_path.exists():
            logger.error("All session files corrupt for %s — returning None", session_id)
        return None

    async def save_data(self, session_data: SessionData):
        """Persist already-materialized SessionData to disk."""
        session_path = self._get_session_path(session_data.session_id)
        serialized = self._serialize_session(session_data.to_dict())
        self.atomic_save(session_path, serialized)

    # ── File-level locking (cross-process) ───────────────────────

    def _get_lock_path(self, session_id: str) -> Path:
        """Return the flock path for a session."""
        return self.sessions_dir / f".{session_id}.lock"

    @contextmanager
    def file_lock(self, session_id: str, *, timeout: float = 10.0, blocking: bool = True):
        """Cross-process file lock using fcntl.flock.

        Usage:
            with persistence.file_lock("session_123") as acquired:
                if acquired:
                    ...  # exclusive access
        """
        lock_path = self._get_lock_path(session_id)
        fd = None
        acquired = False
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            if blocking:
                # Use non-blocking poll with timeout
                import time
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except (OSError, BlockingIOError):
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (OSError, BlockingIOError):
                    pass
            yield acquired
        finally:
            if fd is not None:
                if acquired:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                os.close(fd)

    async def update_atomic(
        self,
        session_id: str,
        mutator: Callable[[SessionData], None],
        *,
        timeout: float = 10.0,
        blocking: bool = True,
    ) -> Optional[SessionData]:
        """Atomic read-modify-write for a session.

        Protocol: flock → read disk → mutator(session) → revision++ → atomic save → unlock.

        Args:
            session_id: Session ID.
            mutator: Callable that modifies SessionData in-place.
            timeout: Max seconds to wait for file lock.
            blocking: If False, return None immediately when lock unavailable.

        Returns:
            Updated SessionData on success, None if lock was not acquired.
        """
        with self.file_lock(session_id, timeout=timeout, blocking=blocking) as acquired:
            if not acquired:
                return None
            # Read latest from disk (single source of truth)
            current = await self.load(session_id)
            if current is None:
                current = SessionData(
                    session_id=session_id,
                    agent_name="",
                    model_name="",
                    session_type=SessionManager.infer_session_type(session_id),
                    history_messages=[],
                    mailbox=[],
                    variables={},
                    created_at=datetime.now().isoformat(),
                    updated_at=datetime.now().isoformat(),
                    state="active",
                    archived_at=None,
                    revision=0,
                )
            # Apply mutation
            mutator(current)
            # Bump revision and timestamp
            current.revision = (current.revision or 0) + 1
            current.updated_at = datetime.now().isoformat()
            # Atomic write
            session_path = self._get_session_path(session_id)
            serialized = self._serialize_session(current.to_dict())
            self.atomic_save(session_path, serialized)
            logger.debug("Session updated atomically: %s (rev=%d)", session_id, current.revision)
            return current

    async def delete(self, session_id: str):
        """删除 Session 文件"""
        session_file = self._get_session_path(session_id)
        if session_file.exists():
            session_file.unlink()
            logger.info(f"Session 文件已删除: {session_id}")

    async def restore_to_agent(self, agent: Any, session_data: SessionData):
        """
        恢复 Session 数据到 Agent

        Args:
            agent: DolphinAgent 实例
            session_data: Session 数据
        """
        try:
            context = agent.executor.context

            # 1. 恢复变量（跳过配置派生变量，它们应从磁盘配置文件重新构建）
            _NON_RESTORABLE_VARS = {"workspace_instructions"}
            for name, value in session_data.variables.items():
                if name in _NON_RESTORABLE_VARS:
                    continue
                if value is not None:
                    context.set_variable(name, value)

            # 2. 恢复历史消息
            if session_data.history_messages:
                compacted_history = DolphinStateAdapter.compact_session_state(
                    session_data.history_messages,
                    max_messages=self.MAX_RESTORED_HISTORY_MESSAGES,
                )
                if len(compacted_history) < len(session_data.history_messages):
                    logger.info(
                        "Truncating restored history from %s to last %s messages for session_id=%s.",
                        len(session_data.history_messages),
                        self.MAX_RESTORED_HISTORY_MESSAGES,
                        session_data.session_id,
                    )
                issues = DolphinStateAdapter.validate_session_state({"history_messages": compacted_history})
                if issues:
                    logger.warning(
                        "Detected %s history sequence issues before restore for session_id=%s. "
                        "Using compacted suffix to preserve protocol validity.",
                        len(issues),
                        session_data.session_id,
                    )

                DolphinStateAdapter.import_session_state(
                    agent,
                    {"history_messages": compacted_history},
                    max_messages=None,
                )

            # 3. 设置 session ID as a variable
            context.set_variable("session_id", session_data.session_id)
            if hasattr(context, "set_session_id"):
                context.set_session_id(session_data.session_id)

            logger.info(f"Session 已恢复: {session_data.session_id}, "
                       f"历史消息: {len(session_data.history_messages)} 条")

        except Exception as e:
            logger.error(f"恢复 Session 失败: {e}")
            raise


class SessionManager:
    """
    Session 管理器

    带并发控制的 Session 管理。
    """

    MAX_TIMELINE_EVENTS = 500  # 防止内存泄漏，限制每个 session 的 timeline 事件数
    _MAX_CACHED_LOCKS = 200

    @staticmethod
    def get_primary_session_id(agent_name: str) -> str:
        """Return the canonical long-lived session id for one agent."""
        return f"web_session_{agent_name}"

    @staticmethod
    def infer_session_type(session_id: str) -> str:
        """Infer runtime session type from session id."""
        sid = str(session_id or "")
        if sid.startswith("heartbeat_session_"):
            return "heartbeat"
        if sid.startswith("job_"):
            return "job"
        if sid.startswith("web_session_") and "__" in sid:
            return "sub"
        if sid.startswith("web_session_"):
            return "primary"
        return "primary"

    @staticmethod
    def get_heartbeat_session_id(agent_name: str) -> str:
        """Return heartbeat-only session id for one agent."""
        return f"heartbeat_session_{agent_name}"

    @staticmethod
    def get_session_prefix(agent_name: str) -> str:
        """Return the session id prefix for one agent."""
        return f"web_session_{agent_name}"

    @staticmethod
    def resolve_agent_name(session_id: str) -> Optional[str]:
        """Extract agent name from a session ID."""
        if session_id.startswith("web_session_"):
            # Matches web_session_{agent_name} or web_session_{agent_name}__suffix
            rem = session_id[len("web_session_"):]
            if "__" in rem:
                return rem.split("__")[0]
            return rem
        return None

    @classmethod
    def is_valid_agent_session_id(cls, agent_name: str, session_id: str) -> bool:
        """Validate one session id belongs to the given agent namespace."""
        if not SessionPersistence.is_safe_session_id(session_id):
            return False
        primary = cls.get_primary_session_id(agent_name)
        if session_id == primary:
            return True
        if session_id.startswith(primary):
            suffix = session_id[len(primary):]
            if bool(suffix) and suffix[0] in "._-":
                return True
        # Also accept non-web channel sessions (tg_session_, discord_session_, etc.)
        from ..channel.session_resolver import ChannelSessionResolver
        for channel_type, prefix in ChannelSessionResolver._PREFIX_MAP.items():
            if channel_type == "web":
                continue
            expected = f"{prefix}{agent_name}{ChannelSessionResolver._SEP}"
            if session_id.startswith(expected):
                return True
        return False

    @classmethod
    def create_chat_session_id(cls, agent_name: str) -> str:
        """Create a new chat session id for one agent."""
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        short = uuid.uuid4().hex[:8]
        return f"{cls.get_session_prefix(agent_name)}__{ts}_{short}"

    def __init__(self, sessions_dir: Path):
        self.persistence = SessionPersistence(sessions_dir)
        self._agents: Dict[str, Any] = {}  # session_id -> DolphinAgent
        self._locks: Dict[str, asyncio.Lock] = {}  # session_id -> Lock
        self._agent_metadata: Dict[str, Dict[str, str]] = {}  # session_id -> metadata
        self._timeline_events: Dict[str, list] = {}  # session_id -> timeline events
        self._timeline_lock = threading.Lock()  # 保护 timeline 操作的线程锁
        self._metrics: Dict[str, float] = {}
        self._metrics_lock = threading.Lock()

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
        """Return True when event is marked stale and exceeds max age."""
        if not bool(event.get("suppress_if_stale", False)):
            return False
        event_ts = self._parse_iso_datetime(event.get("timestamp"))
        if event_ts is None:
            return False
        return (now_utc - event_ts) > stale_after

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """获取 Session 锁（懒创建，带 LRU 淘汰）"""
        if session_id not in self._locks:
            if len(self._locks) >= self._MAX_CACHED_LOCKS:
                to_remove = [k for k, v in self._locks.items() if not v.locked()]
                for k in to_remove[:len(to_remove) // 2]:
                    del self._locks[k]
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

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
            logger.warning(f"获取 Session 锁超时: {session_id}")
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
        """Extract a JSON-serializable context trace from agent if available."""
        if agent is None or not hasattr(agent, "get_execution_trace"):
            return {}
        try:
            raw_trace = agent.get_execution_trace()
            if isinstance(raw_trace, dict):
                return raw_trace
            if isinstance(raw_trace, str):
                parsed = json.loads(raw_trace)
                return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
        return {}

    async def update_atomic(
        self,
        session_id: str,
        mutator: Callable[[SessionData], None],
        *,
        timeout: float = 10.0,
        blocking: bool = True,
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
            )
        finally:
            lock.release()

    async def deposit_mailbox_event(
        self,
        session_id: str,
        event: Dict[str, Any],
        *,
        timeout: float = 5.0,
        blocking: bool = True,
    ) -> bool:
        """Append one event into session mailbox atomically with idempotency."""
        if not isinstance(event, dict):
            return False

        event_obj = dict(event)
        now_utc = datetime.now(timezone.utc)
        if not isinstance(event_obj.get("timestamp"), str) or not str(event_obj.get("timestamp")).strip():
            event_obj["timestamp"] = now_utc.isoformat()
        event_id = str(event_obj.get("event_id") or "").strip()
        dedupe_key = str(event_obj.get("dedupe_key") or "").strip()
        inserted = {"value": False}
        dropped_duplicate = {"value": False}
        dropped_stale = {"value": False}

        def _mutator(session_data: SessionData) -> None:
            if not isinstance(session_data.mailbox, list):
                session_data.mailbox = []
            mailbox = [e for e in session_data.mailbox if isinstance(e, dict)]

            if event_id:
                existing_ids = {str(e.get("event_id") or "").strip() for e in mailbox}
                if event_id in existing_ids:
                    dropped_duplicate["value"] = True
                    return

            if self._is_mailbox_event_stale(event_obj, now_utc=now_utc):
                dropped_stale["value"] = True
                return

            if dedupe_key:
                filtered = []
                removed_any = False
                for existing in mailbox:
                    existing_key = str(existing.get("dedupe_key") or "").strip()
                    if existing_key and existing_key == dedupe_key:
                        removed_any = True
                        continue
                    filtered.append(existing)
                mailbox = filtered
                if removed_any:
                    dropped_duplicate["value"] = True

            mailbox.append(dict(event_obj))
            session_data.mailbox = mailbox
            inserted["value"] = True

        updated = await self.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking)
        if updated is None:
            return False
        if inserted["value"]:
            self.record_metric("mailbox_deposit_count")
        if dropped_duplicate["value"]:
            self.record_metric("mailbox_dedup_drop_count")
        if dropped_stale["value"]:
            self.record_metric("mailbox_stale_drop_count")
        return True

    async def inject_history_message(
        self,
        session_id: str,
        message: dict,
        *,
        timeout: float = 5.0,
        blocking: bool = True,
    ) -> bool:
        """Append one message into session history_messages atomically.

        Used by HeartbeatRunner to inject deliverable results into the
        primary session's conversation history so that subsequent chat
        turns see the heartbeat output as a real assistant message.
        """
        if not isinstance(message, dict):
            return False

        msg_obj = dict(message)

        def _mutator(session_data: SessionData) -> None:
            if not isinstance(session_data.history_messages, list):
                session_data.history_messages = []
            session_data.history_messages.append(msg_obj)

        updated = await self.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking)
        if updated is not None:
            self.record_metric("history_inject_count")
        return updated is not None

    async def ack_mailbox_events(
        self,
        session_id: str,
        event_ids: list[str],
        *,
        timeout: float = 5.0,
        blocking: bool = True,
    ) -> bool:
        """Remove consumed mailbox events by event_id atomically."""
        ids = {str(eid).strip() for eid in event_ids if str(eid).strip()}
        if not ids:
            return True

        def _mutator(session_data: SessionData) -> None:
            if not isinstance(session_data.mailbox, list):
                session_data.mailbox = []
            session_data.mailbox = [
                e for e in session_data.mailbox
                if not isinstance(e, dict) or str(e.get("event_id") or "").strip() not in ids
            ]

        updated = await self.update_atomic(session_id, _mutator, timeout=timeout, blocking=blocking)
        if updated is not None:
            self.record_metric("mailbox_drain_count", float(len(ids)))
        return updated is not None

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
    ):
        """保存 Session.

        默认走 ``update_atomic`` 统一写入口。
        在调用方已持有 session 的进程内锁与文件锁时，可设置
        ``lock_already_held=True`` 避免重入锁导致死锁。
        """
        logger.debug("Persisting session %s to disk", session_id)
        timeline = self.get_timeline(session_id)
        context_trace = self._extract_context_trace(agent)

        if lock_already_held:
            await self.persistence.save(
                session_id,
                agent,
                model_name,
                timeline=timeline,
                context_trace=context_trace,
            )
            logger.debug("Session persisted.")
            return

        context = agent.executor.context
        exported_state = DolphinStateAdapter.export_session_state(agent)
        serializable_history = exported_state.get("history_messages", [])
        created_at_hint = context.get_var_value("session_created_at")

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
            session_data.variables = {
                "workspace_instructions": context.get_var_value("workspace_instructions"),
                "model_name": context.get_var_value("model_name"),
                "current_time": context.get_var_value("current_time"),
            }
            if not session_data.created_at:
                session_data.created_at = created_at_hint or datetime.now().isoformat()
            session_data.timeline = timeline or []
            session_data.context_trace = context_trace or {}

        updated = await self.update_atomic(session_id, _mutator, timeout=10.0, blocking=True)
        if updated is None:
            raise TimeoutError(f"Failed to persist session {session_id}: lock not acquired")
        print(f"[Chat] Session persisted.")

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
                target.updated_at = datetime.now().isoformat()
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
                target.updated_at = datetime.now().isoformat()

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
        session_data.updated_at = datetime.now().isoformat()
        await self.persistence.save_data(session_data)
        # Also clear in-memory caches so next load is fresh
        self._agents.pop(session_id, None)
        self._agent_metadata.pop(session_id, None)
        self._timeline_events.pop(session_id, None)
        logger.info("Session history cleared: %s", session_id)
        return True

    async def reset_session(self, session_id: str):
        """重置 Session：清除缓存和磁盘文件"""
        self._agents.pop(session_id, None)
        self._agent_metadata.pop(session_id, None)
        self._timeline_events.pop(session_id, None)
        await self.persistence.delete(session_id)
        logger.info(f"Session 已重置: {session_id}")

    async def reset_agent_sessions(self, agent_name: str) -> int:
        """Reset all sessions for one agent and return removed count."""
        prefix = self.get_session_prefix(agent_name)
        targets = [p.stem for p in self.persistence.sessions_dir.glob(f"{prefix}*.json")]
        removed = 0
        for session_id in targets:
            await self.reset_session(session_id)
            removed += 1
        for session_id in list(self._agent_metadata.keys()):
            meta = self._agent_metadata.get(session_id) or {}
            if meta.get("agent_name") == agent_name:
                self._agents.pop(session_id, None)
                self._agent_metadata.pop(session_id, None)
                self._timeline_events.pop(session_id, None)
        return removed

    async def restore_to_agent(self, agent: Any, session_data: SessionData):
        """恢复 Session 到 Agent"""
        await self.persistence.restore_to_agent(agent, session_data)

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
