"""Session scanner — detects new sessions for reflection skills."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .base import BaseScanner, ScanResult

logger = logging.getLogger(__name__)

# Session types to scan (user conversations and jobs, not heartbeat/workflow internals)
_SCANNABLE_PREFIXES = ("web_session_", "job_")
_SKIP_PREFIXES = ("heartbeat_session_", "workflow_")


@dataclass
class SessionSummary:
    """Lightweight summary of a session file."""

    id: str  # session_id
    path: Path  # Session JSON file path
    updated_at: str  # ISO timestamp from SessionData.updated_at
    session_type: str  # primary, job, etc.


class SessionScanner(BaseScanner):
    """Scanner for session files.

    Detects new/updated sessions since last watermark for reflection skills.
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)

    def check(self, watermark: str, agent_name: str = "") -> ScanResult:
        """Lightweight pre-check: count sessions newer than watermark.

        Returns ScanResult with payload=list[SessionSummary] when has_changes=True.
        Exceptions bubble up to heartbeat layer for scanner_error event logging.
        """
        sessions = self.get_reviewable_sessions(watermark, agent_name=agent_name)
        if not sessions:
            return ScanResult(
                has_changes=False,
                change_summary="No new sessions since last scan",
            )
        return ScanResult(
            has_changes=True,
            change_summary=f"{len(sessions)} new sessions since last scan",
            payload=sessions,
        )

    def get_reviewable_sessions(
        self,
        watermark: str,
        agent_name: str = "",
        max_sessions: int = 5,
        max_age_days: int = 7,
    ) -> List[SessionSummary]:
        """List sessions to scan, filtered and sorted by updated_at ascending.

        Args:
            watermark: ISO timestamp, only sessions with updated_at > watermark.
            agent_name: If non-empty, only scan this agent's sessions (prefix match).
            max_sessions: Maximum number of sessions to return.
            max_age_days: Skip sessions older than this many days.
        """
        if not self.sessions_dir.exists():
            return []

        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        # Use start-of-day for age cutoff so the filter has day-level
        # granularity — "7 days ago" means any time on that calendar day.
        cutoff_dt = (now - timedelta(days=max_age_days)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        age_mtime_cutoff = cutoff_dt.timestamp()
        # Use watermark as mtime lower bound: files not modified since watermark
        # cannot have updated_at > watermark, so skip them without reading content.
        wm_mtime_cutoff = 0.0
        if watermark:
            try:
                wm_dt = datetime.fromisoformat(watermark)
                if wm_dt.tzinfo is None:
                    wm_dt = wm_dt.replace(tzinfo=timezone.utc)
                wm_mtime_cutoff = wm_dt.timestamp()
            except (ValueError, TypeError):
                pass
        mtime_cutoff = max(age_mtime_cutoff, wm_mtime_cutoff)

        results: List[SessionSummary] = []

        for path in self.sessions_dir.glob("*.json"):
            if path.name.endswith((".bak", ".tmp", ".lock")):
                continue

            session_id = path.stem

            # Filter by session type
            if not any(session_id.startswith(p) for p in _SCANNABLE_PREFIXES):
                continue
            if any(session_id.startswith(p) for p in _SKIP_PREFIXES):
                continue

            # Filter by agent name if specified
            if agent_name:
                if not self._matches_agent(session_id, agent_name):
                    continue

            # Pre-filter by file mtime to avoid reading stale files
            try:
                stat = path.stat()
                if stat.st_mtime < mtime_cutoff:
                    continue
            except OSError:
                continue

            # Metadata extraction (read file content for accurate updated_at)
            try:
                meta = self._read_session_meta(path)
                if meta is None:
                    continue
            except Exception as e:
                logger.debug("Failed to read session %s: %s", session_id, e)
                continue

            updated_at = meta.get("updated_at", "")
            if not updated_at:
                continue

            # Watermark filter
            if watermark and updated_at <= watermark:
                continue

            # Age filter — parse updated_at to datetime for correct comparison
            # (avoids string comparison pitfalls with timezone suffixes)
            try:
                ua_dt = datetime.fromisoformat(updated_at)
                if ua_dt.tzinfo is None:
                    ua_dt = ua_dt.replace(tzinfo=timezone.utc)
                if ua_dt < cutoff_dt:
                    continue
            except (ValueError, TypeError):
                continue

            session_type = meta.get("session_type", "")
            results.append(SessionSummary(
                id=session_id,
                path=path,
                updated_at=updated_at,
                session_type=session_type,
            ))

        # Sort by updated_at ascending (oldest first)
        results.sort(key=lambda s: s.updated_at)

        return results[:max_sessions]

    def extract_digest(
        self,
        session_path: Path,
        max_messages: int = 40,
        max_chars: int = 4000,
    ) -> str:
        """Extract a compact text digest from a session file.

        Format: [user] {text}\n[assistant] {text}\n...
        Only includes user and assistant text content.
        """
        data = self._load_session_data(session_path)
        if data is None:
            raise ValueError(f"Failed to load session: {session_path}")

        messages = data.get("history_messages", [])
        lines: List[str] = []
        total_chars = 0

        for msg in messages[-max_messages:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue

            text = self._extract_text_content(msg)
            if not text:
                continue

            line = f"[{role}] {text}"
            if total_chars + len(line) > max_chars:
                # Truncate to fit
                remaining = max_chars - total_chars
                if remaining > 20:
                    lines.append(line[:remaining] + "...")
                break
            lines.append(line)
            total_chars += len(line) + 1  # +1 for newline

        return "\n".join(lines)

    def load_session_messages(self, session_id: str) -> List[dict]:
        """Load complete history_messages for a session."""
        path = self.sessions_dir / f"{session_id}.json"
        data = self._load_session_data(path)
        if data is None:
            raise ValueError(f"Failed to load session: {session_id}")
        return data.get("history_messages", [])

    @staticmethod
    def _matches_agent(session_id: str, agent_name: str) -> bool:
        """Check if session_id belongs to the given agent."""
        # Patterns: web_session_{agent_name}, web_session_{agent_name}_{uuid}
        # job_{agent_name}_{...}
        return (
            session_id.startswith(f"web_session_{agent_name}")
            or session_id.startswith(f"job_{agent_name}")
        )

    @staticmethod
    def _read_session_meta(path: Path) -> Optional[dict]:
        """Read minimal metadata from session file without loading full content."""
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            # Remove checksum for validation-free quick read
            data.pop("_checksum", None)
            return {
                "updated_at": data.get("updated_at", ""),
                "session_type": data.get("session_type", ""),
                "session_id": data.get("session_id", ""),
            }
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to read session meta from %s: %s", path, e)
            return None

    @staticmethod
    def _load_session_data(path: Path) -> Optional[dict]:
        """Load full session data from file."""
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            data.pop("_checksum", None)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to load session data from %s: %s", path, e)
            return None

    @staticmethod
    def _extract_text_content(msg: dict) -> str:
        """Extract text from message content (handles string or list format)."""
        content = msg.get("content")
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            # Extract only text blocks
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text.strip())
            return " ".join(parts)
        return ""
