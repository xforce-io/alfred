"""Session persistence — atomic file I/O, locking, and session restore."""

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
import logging

from ...infra.dolphin_state_adapter import DolphinStateAdapter
from .compressor import SessionCompressor
from .session_data import SessionData
from . import session_ids as _sid

logger = logging.getLogger(__name__)


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
            # Best-effort: fsync directory is POSIX-only; data file already fsynced above.
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
        trailing_messages: Optional[List[Dict[str, Any]]] = None,
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
            portable = agent.snapshot.export_portable_session()
            serializable_history = portable.get("history_messages", [])
            if trailing_messages:
                serializable_history = list(serializable_history) + list(trailing_messages)
            exported_variables = portable.get("variables", {})
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
                session_type=_sid.infer_session_type(session_id),
                history_messages=serializable_history,
                mailbox=(previous.mailbox if previous and isinstance(previous.mailbox, list) else []),
                variables=exported_variables,
                created_at=created_at,
                updated_at=datetime.now().isoformat(),
                state=(previous.state if previous and isinstance(previous.state, str) else "active"),
                archived_at=(previous.archived_at if previous else None),
                timeline=timeline or [],
                context_trace=context_trace or {},
                revision=next_revision,
            )

            # Compress history for primary sessions before persisting.
            if data.session_type == "primary":
                try:
                    compressor = SessionCompressor(agent.executor.context)
                    compressed, new_history = await compressor.maybe_compress(data.history_messages)
                    if compressed:
                        data.history_messages = new_history
                except Exception:
                    logger.warning("History compression failed; saving uncompressed", exc_info=True)

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
            data["session_type"] = _sid.infer_session_type(str(data.get("session_id") or ""))
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
                        # fd is about to close; OS will implicitly release the lock.
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
                    session_type=_sid.infer_session_type(session_id),
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
            # 1. Compact history (SDK has no max_messages truncation)
            compacted_history = session_data.history_messages or []
            if compacted_history:
                compacted_history = DolphinStateAdapter.compact_session_state(
                    compacted_history,
                    max_messages=self.MAX_RESTORED_HISTORY_MESSAGES,
                )
                if len(compacted_history) < len(session_data.history_messages):
                    logger.info(
                        "Truncating restored history from %s to last %s messages for session_id=%s.",
                        len(session_data.history_messages),
                        self.MAX_RESTORED_HISTORY_MESSAGES,
                        session_data.session_id,
                    )

            # 2. Build portable state, filtering non-restorable variables
            _NON_RESTORABLE_VARS = {"workspace_instructions"}
            restore_variables = {
                k: v for k, v in (session_data.variables or {}).items()
                if k not in _NON_RESTORABLE_VARS and v is not None
            }

            portable_state = {
                "schema_version": "portable_session.v1",
                "session_id": session_data.session_id,
                "history_messages": compacted_history,
                "variables": restore_variables,
            }

            # 3. Import via SDK (handles history, variables, session_id, and repair)
            agent.snapshot.import_portable_session(portable_state, repair=True)

            logger.info(f"Session 已恢复: {session_data.session_id}, "
                       f"历史消息: {len(compacted_history)} 条")

        except Exception as e:
            logger.error(f"恢复 Session 失败: {e}")
            raise
