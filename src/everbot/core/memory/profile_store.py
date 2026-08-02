"""Profile memory store — parse and persist MEMORY.md.

Profile memories describe **who the user is** (preferences, facts, workflows).
They are evergreen, score-decayed, and partitioned into Active/Archived
sections within a single MEMORY.md file per agent.

Event memories live in a separate ``event_store`` module — they are
time-anchored, append-only, and stored under ``events/YYYY-MM.md``.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import MemoryEntry

logger = logging.getLogger(__name__)

# Keep the legacy header stable. New relation fields live in an optional
# HTML metadata comment on the following line so old readers still see entries.
_HEADER_RE = re.compile(
    r"^###\s+\[(\w+)\]\s+(\w+)\s*\|\s*([\d.]+)\s*\|\s*([\d-]+)\s*\|\s*(\d+)\s*$"
)
_ENTRY_META_RE = re.compile(r"^<!--\s*memory_meta:\s*(\{.*\})\s*-->$")
_META_PROCESSED_RE = re.compile(r"<!--\s*last_processed_count:\s*(\d+)\s*-->")


class ProfileStore:
    """Read / write profile memory entries from MEMORY.md."""

    def __init__(self, memory_path: Path):
        self.memory_path = Path(memory_path)
        self.last_processed_count: int = 0

    def load(self) -> List[MemoryEntry]:
        """Parse MEMORY.md into MemoryEntry list. Tolerant of corruption."""
        if not self.memory_path.exists():
            return []

        try:
            text = self.memory_path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read %s", self.memory_path, exc_info=True)
            return []

        if not text.strip():
            return []

        # Parse metadata
        m = _META_PROCESSED_RE.search(text)
        if m:
            self.last_processed_count = int(m.group(1))

        entries: List[MemoryEntry] = []
        current_entry: Optional[dict] = None
        content_lines: List[str] = []

        for line in text.split("\n"):
            m = _HEADER_RE.match(line.strip())
            if m:
                # Flush previous entry
                if current_entry is not None:
                    current_entry["content"] = "\n".join(content_lines).strip()
                    if current_entry["content"]:
                        try:
                            entries.append(MemoryEntry.from_dict(current_entry))
                        except Exception:
                            logger.debug("Skipping corrupt entry: %s", current_entry.get("id"))

                current_entry = {
                    "id": m.group(1),
                    "category": m.group(2),
                    "score": float(m.group(3)),
                    "last_activated": m.group(4),
                    "activation_count": int(m.group(5)),
                    "kind": "profile",
                }
                content_lines = []
            elif current_entry is not None:
                stripped = line.strip()
                meta_match = _ENTRY_META_RE.match(stripped)
                if meta_match:
                    try:
                        metadata = json.loads(meta_match.group(1))
                        current_entry.update({
                            "status": metadata.get("status", "active"),
                            "supersedes": metadata.get("supersedes", []),
                            "superseded_by": metadata.get("superseded_by", []),
                            "source_session": metadata.get("source_session", ""),
                        })
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Skipping malformed memory relation metadata")
                    continue
                # Skip section headers (# or ##) — they're structural, not content
                if stripped.startswith("## ") or stripped.startswith("# "):
                    continue
                content_lines.append(line)

        # Flush last entry
        if current_entry is not None:
            current_entry["content"] = "\n".join(content_lines).strip()
            if current_entry["content"]:
                try:
                    entries.append(MemoryEntry.from_dict(current_entry))
                except Exception:
                    logger.debug("Skipping corrupt entry: %s", current_entry.get("id"))

        return entries

    # Hard cap on total entries to prevent unbounded growth.
    # Increase once consolidation logic matures.
    MAX_ENTRIES = 30

    def save(self, entries: List[MemoryEntry], last_processed_count: Optional[int] = None) -> None:
        """Atomically write entries to MEMORY.md while preserving superseded trace."""
        if last_processed_count is not None:
            self.last_processed_count = last_processed_count
        # Drop only near-zero active entries. Superseded entries are retained
        # for provenance even though they are never injected.
        entries = [e for e in entries if e.status == "superseded" or e.score >= 0.05]

        # Enforce hard cap: keep top entries by score
        if len(entries) > self.MAX_ENTRIES:
            active_ranked = sorted(
                [e for e in entries if e.status == "active"],
                key=lambda e: e.score,
                reverse=True,
            )
            trace_ranked = sorted(
                [e for e in entries if e.status == "superseded"],
                key=lambda e: e.last_activated,
                reverse=True,
            )
            trace_budget = min(len(trace_ranked), 5)
            active_budget = self.MAX_ENTRIES - trace_budget
            entries = active_ranked[:active_budget] + trace_ranked[:trace_budget]

        # Partition
        active = sorted(
            [e for e in entries if e.status == "active" and e.score >= 0.2],
            key=lambda e: e.score,
            reverse=True,
        )
        archived = sorted(
            [e for e in entries if e.status == "superseded" or e.score < 0.2],
            key=lambda e: e.score,
            reverse=True,
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        total = len(active) + len(archived)

        lines = [
            "# Agent Memory",
            "",
            f"<!-- Last updated: {now_str} -->",
            f"<!-- Total entries: {total} -->",
            f"<!-- last_processed_count: {self.last_processed_count} -->",
            "",
        ]

        if active:
            lines.append("## Active Memories")
            lines.append("")
            for entry in active:
                lines.append(_format_entry(entry))

        if archived:
            lines.append("## Archived Memories")
            lines.append("")
            for entry in archived:
                lines.append(_format_entry(entry))

        content = "\n".join(lines)

        # Ensure parent dir exists
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)

        # Backup existing file
        if self.memory_path.exists():
            bak = self.memory_path.with_suffix(".md.bak")
            try:
                bak.write_text(self.memory_path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                logger.debug("Backup failed", exc_info=True)

        tmp = self.memory_path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, self.memory_path)


def _format_entry(entry: MemoryEntry) -> str:
    """Format a single entry as markdown block."""
    date_str = entry.last_activated[:10] if len(entry.last_activated) >= 10 else entry.last_activated
    header = (
        f"### [{entry.id}] {entry.category} | {entry.score:.2f} | "
        f"{date_str} | {entry.activation_count}"
    )
    metadata = json.dumps({
        "status": entry.status,
        "supersedes": entry.supersedes,
        "superseded_by": entry.superseded_by,
        "source_session": entry.source_session,
    }, ensure_ascii=False, separators=(",", ":"))
    return f"{header}\n<!-- memory_meta: {metadata} -->\n{entry.content}\n"
