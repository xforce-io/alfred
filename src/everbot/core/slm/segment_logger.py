"""Skill invocation log — pointer-based storage with lazy resolution."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from .models import EvaluationSegment, SkillLogEntry

logger = logging.getLogger(__name__)

# Retention: keep last 500 entries or 90 days (whichever is reached first)
_MAX_ENTRIES = 500
_MAX_AGE_DAYS = 90


class SegmentLogger:
    """Append-only JSONL logger for skill invocation pointers.

    Storage: ``{logs_dir}/{skill_id}.jsonl`` — each line is a
    :class:`SkillLogEntry` (lightweight pointer, not full content).
    """

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def _log_path(self, skill_id: str) -> Path:
        return self._logs_dir / f"{skill_id}.jsonl"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, entry: SkillLogEntry) -> None:
        """Append a pointer entry to the skill's log file."""
        path = self._log_path(entry.skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, skill_id: str) -> List:
        """Load all entries for a skill.

        Returns EvaluationSegment objects for entries with inline content (new
        format written by SkillLogRecorder) and SkillLogEntry objects for legacy
        pointer-only entries (identified by presence of run_id without content).
        """
        path = self._log_path(skill_id)
        if not path.exists():
            return []
        entries: List = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                # Legacy pointer format: has run_id but no inline content
                if data.get("run_id") and not data.get("context_before") and not data.get("skill_output"):
                    entries.append(SkillLogEntry.from_dict(data))
                else:
                    entries.append(EvaluationSegment.from_dict(data))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping malformed entry line: %s", e)
        return entries

    def load_by_version(self, skill_id: str, version: str) -> List:
        """Load entries for a specific skill version."""
        return [e for e in self.load(skill_id) if e.skill_version == version]

    def count(self, skill_id: str) -> int:
        """Count entries without loading all data."""
        path = self._log_path(skill_id)
        if not path.exists():
            return 0
        count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    # ------------------------------------------------------------------
    # Resolve — convert pointers to full EvaluationSegments
    # ------------------------------------------------------------------

    def resolve(
        self,
        entries: List,
        sessions_dir: Path,
    ) -> List[EvaluationSegment]:
        """Resolve entries to full EvaluationSegments.

        If an entry is already an EvaluationSegment (inline content written by
        SkillLogRecorder), it is returned directly.  Legacy SkillLogEntry pointer
        entries are resolved by reading the session file.

        Entries whose session files are missing or unreadable are silently skipped.
        """
        segments: List[EvaluationSegment] = []
        # Cache loaded sessions to avoid re-reading for multiple entries
        session_cache: dict[str, Optional[dict]] = {}

        for entry in entries:
            if isinstance(entry, EvaluationSegment):
                segments.append(entry)
                continue
            # Legacy SkillLogEntry pointer path: read session file
            session = self._load_session(entry.session_id, sessions_dir, session_cache)
            if session is None:
                continue
            seg = self._extract_segment(entry, session)
            if seg is not None:
                segments.append(seg)
        return segments

    @staticmethod
    def _load_session(
        session_id: str,
        sessions_dir: Path,
        cache: dict[str, Optional[dict]],
    ) -> Optional[dict]:
        """Load and cache a session JSON file."""
        if session_id in cache:
            return cache[session_id]

        # Try common session file naming patterns
        candidates = list(sessions_dir.glob(f"*{session_id}*.json"))
        # Filter out .bak files and substring collisions
        candidates = [p for p in candidates if not p.name.endswith(".bak")]

        for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("session_id") == session_id:
                    cache[session_id] = data
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Cannot read session file %s: %s", path, e)

        cache[session_id] = None
        return None

    @staticmethod
    def _extract_segment(
        entry: SkillLogEntry,
        session: dict,
    ) -> Optional[EvaluationSegment]:
        """Extract context from a session using timeline + history_messages."""
        timeline = session.get("timeline", [])
        messages = session.get("history_messages", [])

        # Find the skill's turn by matching run_id in timeline
        # Locate turn boundaries: find turn_start and turn_end with same run_id
        turn_indices: list[int] = []
        for i, evt in enumerate(timeline):
            if evt.get("run_id") == entry.run_id:
                turn_indices.append(i)

        if not turn_indices:
            return None

        # Find this turn's position among all turns
        all_turn_starts = [
            i for i, evt in enumerate(timeline)
            if evt.get("type") == "turn_start"
        ]
        current_turn_start = None
        for ts_idx in all_turn_starts:
            if ts_idx <= turn_indices[0]:
                current_turn_start = ts_idx

        if current_turn_start is None:
            return None

        turn_position = all_turn_starts.index(current_turn_start)

        # Extract context from history_messages
        # Each turn ≈ 2 messages (user + assistant), so turn N maps to messages[2N:2N+2]
        msg_idx = turn_position * 2

        # context_before: the user message that triggered this turn
        context_before = ""
        if 0 <= msg_idx < len(messages):
            ctx = messages[msg_idx].get("content", "")
            context_before = ctx if isinstance(ctx, str) else str(ctx)[:2000]

        # skill_output: the assistant response containing the skill output
        skill_output = ""
        if 0 <= msg_idx + 1 < len(messages):
            ctx = messages[msg_idx + 1].get("content", "")
            skill_output = ctx if isinstance(ctx, str) else str(ctx)[:4000]

        # context_after: the next user message (reaction)
        context_after = ""
        if 0 <= msg_idx + 2 < len(messages):
            ctx = messages[msg_idx + 2].get("content", "")
            context_after = ctx if isinstance(ctx, str) else str(ctx)[:2000]

        return EvaluationSegment(
            skill_id=entry.skill_id,
            skill_version=entry.skill_version,
            triggered_at=entry.triggered_at,
            context_before=context_before,
            skill_output=skill_output,
            context_after=context_after,
            session_id=entry.session_id,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, skill_id: str) -> int:
        """Remove entries exceeding retention limits.

        Returns number of entries removed.
        """
        path = self._log_path(skill_id)
        if not path.exists():
            return 0

        lines = path.read_text(encoding="utf-8").splitlines()
        original_count = len(lines)

        # Parse and filter by age
        cutoff = time.time() - _MAX_AGE_DAYS * 86400
        kept: List[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                triggered = data.get("triggered_at", "")
                if triggered:
                    from datetime import datetime, timezone

                    ts = datetime.fromisoformat(triggered).timestamp()
                    if ts < cutoff:
                        continue
            except (json.JSONDecodeError, ValueError):
                pass  # keep malformed lines to avoid silent data loss
            kept.append(line)

        # Trim to max count (keep most recent)
        if len(kept) > _MAX_ENTRIES:
            kept = kept[-_MAX_ENTRIES:]

        removed = original_count - len(kept)
        if removed > 0:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
            logger.info("Cleaned up %d entries for %s", removed, skill_id)

        return removed

    def list_skills(self) -> List[str]:
        """List skill IDs that have log files."""
        if not self._logs_dir.exists():
            return []
        return sorted(
            p.stem for p in self._logs_dir.glob("*.jsonl")
        )
