"""Markdown-based memory store — parse and persist MEMORY.md."""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import MemoryEntry

logger = logging.getLogger(__name__)

# Header pattern: ### [id] category | score | last_activated_date | activation_count
_HEADER_RE = re.compile(
    r"^###\s+\[(\w+)\]\s+(\w+)\s*\|\s*([\d.]+)\s*\|\s*([\d-]+)\s*\|\s*(\d+)\s*$"
)
_META_PROCESSED_RE = re.compile(r"<!--\s*last_processed_count:\s*(\d+)\s*-->")


class MemoryStore:
    """Read / write structured memory entries from MEMORY.md."""

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
                }
                content_lines = []
            elif current_entry is not None:
                stripped = line.strip()
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

    def save(self, entries: List[MemoryEntry], last_processed_count: Optional[int] = None) -> None:
        """Write entries to MEMORY.md with backup. Discards score < 0.05."""
        if last_processed_count is not None:
            self.last_processed_count = last_processed_count
        # Filter out near-zero entries
        entries = [e for e in entries if e.score >= 0.05]

        # Partition
        active = sorted([e for e in entries if e.score >= 0.2], key=lambda e: e.score, reverse=True)
        archived = sorted([e for e in entries if e.score < 0.2], key=lambda e: e.score, reverse=True)

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

        self.memory_path.write_text(content, encoding="utf-8")


def _format_entry(entry: MemoryEntry) -> str:
    """Format a single entry as markdown block."""
    date_str = entry.last_activated[:10] if len(entry.last_activated) >= 10 else entry.last_activated
    header = f"### [{entry.id}] {entry.category} | {entry.score:.2f} | {date_str} | {entry.activation_count}"
    return f"{header}\n{entry.content}\n"
