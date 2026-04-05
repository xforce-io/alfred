"""Skill invocation log — inline EvaluationSegment storage."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import List

from .models import EvaluationSegment

logger = logging.getLogger(__name__)

# Retention: keep last 500 entries or 90 days (whichever is reached first)
_MAX_ENTRIES = 500
_MAX_AGE_DAYS = 90
_INLINE_OUTPUT_LIMIT_BYTES = 4 * 1024
_INLINE_OUTPUT_SUMMARY_CHARS = 1200


class SegmentLogger:
    """Append-only JSONL logger for skill invocation segments.

    Storage: ``{logs_dir}/{skill_id}.jsonl`` — each line is a
    :class:`EvaluationSegment` with inline content.
    """

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def _log_path(self, skill_id: str) -> Path:
        return self._logs_dir / f"{skill_id}.jsonl"

    def _artifact_dir(self, skill_id: str) -> Path:
        return self._logs_dir / "_artifacts" / skill_id

    def _prepare_segment_for_storage(self, segment: EvaluationSegment) -> EvaluationSegment:
        """Persist oversized output out-of-line and keep a concise inline summary."""
        raw_output = segment.skill_output or ""
        if len(raw_output.encode("utf-8")) <= _INLINE_OUTPUT_LIMIT_BYTES:
            return segment

        artifact_path = self._write_raw_output_artifact(
            skill_id=segment.skill_id,
            session_id=segment.session_id,
            triggered_at=segment.triggered_at,
            content=raw_output,
        )
        summary = self._summarize_output(raw_output)
        return replace(
            segment,
            skill_output=summary,
            output_truncated=True,
            raw_output_path=str(artifact_path),
        )

    def _write_raw_output_artifact(
        self,
        *,
        skill_id: str,
        session_id: str,
        triggered_at: str,
        content: str,
    ) -> Path:
        """Write full output to a deterministic artifact file."""
        artifact_dir = self._artifact_dir(skill_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_triggered = (
            triggered_at.replace(":", "-").replace("/", "-").replace(" ", "_")
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        filename = f"{safe_triggered}_{session_id}_{digest}.txt"
        path = artifact_dir / filename
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
        return path

    @staticmethod
    def _summarize_output(content: str) -> str:
        """Keep a readable inline summary while pointing to the raw artifact."""
        normalized = content.strip()
        if len(normalized) <= _INLINE_OUTPUT_SUMMARY_CHARS:
            return normalized
        return normalized[:_INLINE_OUTPUT_SUMMARY_CHARS].rstrip() + "\n...[truncated]"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, segment: EvaluationSegment) -> None:
        """Append an evaluation segment to the skill's log file."""
        segment = self._prepare_segment_for_storage(segment)
        path = self._log_path(segment.skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(segment.to_json() + "\n")

    def backfill_context_after(
        self, skill_id: str, session_id: str, context_after: str,
    ) -> bool:
        """Fill in context_after for the most recent segment matching *session_id*.

        At write time the user's next reaction is not yet available, so
        :meth:`append` stores ``context_after=""``.  On the **next** user
        message the caller invokes this method to retroactively fill it in.

        Implementation: read all lines, patch the last matching line in-place,
        rewrite the file atomically.  Cheap for ≤500-line JSONL files.

        Returns True if a line was patched, False otherwise.
        """
        path = self._log_path(skill_id)
        if not path.exists():
            return False

        lines = path.read_text(encoding="utf-8").splitlines()
        patched = False
        # Scan backwards to find the most recent matching segment
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("session_id") == session_id and not data.get("context_after"):
                data["context_after"] = context_after
                lines[i] = json.dumps(data, ensure_ascii=False)
                patched = True
                break

        if patched:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)

        return patched

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
                segments.append(EvaluationSegment.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping malformed segment line: %s", e)
        return segments

    def load_by_version(self, skill_id: str, version: str) -> List[EvaluationSegment]:
        """Load segments for a specific skill version."""
        return [s for s in self.load(skill_id) if s.skill_version == version]

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
