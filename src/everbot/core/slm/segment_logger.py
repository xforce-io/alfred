"""Evaluation Segment logger — write and read skill invocation logs."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import List

from .models import EvaluationSegment

logger = logging.getLogger(__name__)

# Retention: keep last 500 segments or 90 days (whichever is reached first)
_MAX_SEGMENTS = 500
_MAX_AGE_DAYS = 90


class SegmentLogger:
    """Append-only JSONL logger for Evaluation Segments.

    Storage: ``{logs_dir}/{skill_id}.jsonl``
    """

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def _log_path(self, skill_id: str) -> Path:
        return self._logs_dir / f"{skill_id}.jsonl"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, segment: EvaluationSegment) -> None:
        """Append a segment to the skill's log file."""
        path = self._log_path(segment.skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(segment.to_json() + "\n")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, skill_id: str) -> List[EvaluationSegment]:
        """Load all segments for a skill."""
        path = self._log_path(skill_id)
        if not path.exists():
            return []
        segments: List[EvaluationSegment] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                segments.append(EvaluationSegment.from_json(line))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping malformed segment line: %s", e)
        return segments

    def load_by_version(self, skill_id: str, version: str) -> List[EvaluationSegment]:
        """Load segments for a specific skill version."""
        return [s for s in self.load(skill_id) if s.skill_version == version]

    def count(self, skill_id: str) -> int:
        """Count segments without loading all data."""
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
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, skill_id: str) -> int:
        """Remove segments exceeding retention limits.

        Returns number of segments removed.
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
        if len(kept) > _MAX_SEGMENTS:
            kept = kept[-_MAX_SEGMENTS:]

        removed = original_count - len(kept)
        if removed > 0:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
            logger.info("Cleaned up %d segments for %s", removed, skill_id)

        return removed

    def list_skills(self) -> List[str]:
        """List skill IDs that have log files."""
        if not self._logs_dir.exists():
            return []
        return sorted(
            p.stem for p in self._logs_dir.glob("*.jsonl")
        )
