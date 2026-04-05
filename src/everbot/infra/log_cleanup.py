"""Utilities for redacting and migrating historical Alfred log data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.slm.models import EvaluationSegment
from ..core.slm.segment_logger import SegmentLogger
from ..core.slm.skill_log_recorder import SkillLogRecorder
from .logging_utils import redact_sensitive_text
from .user_data import UserDataManager


@dataclass
class CleanupSummary:
    """Summary of one cleanup run."""

    files_scanned: int = 0
    files_updated: int = 0
    backups_created: int = 0
    lines_redacted: int = 0
    skill_segments_migrated: int = 0


def cleanup_alfred_logs(
    *,
    user_data: UserDataManager,
    dry_run: bool = True,
    agent_name: str = "",
) -> CleanupSummary:
    """Redact historical logs and migrate legacy skill log segments."""
    summary = CleanupSummary()

    for path in _iter_text_logs(user_data):
        _sanitize_text_file(path, summary=summary, dry_run=dry_run)

    for logs_dir in _iter_skill_log_dirs(user_data, agent_name=agent_name):
        _migrate_skill_log_dir(logs_dir, summary=summary, dry_run=dry_run)

    return summary


def _iter_text_logs(user_data: UserDataManager):
    """Yield plain-text and JSONL log files under ~/.alfred/logs."""
    candidates = [
        user_data.logs_dir / "everbot.out",
        user_data.logs_dir / "heartbeat.log",
        user_data.logs_dir / "heartbeat_events.jsonl",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            yield path
        for rotated in sorted(path.parent.glob(f"{path.name}.*")):
            if rotated.is_file() and ".bak_" not in rotated.name:
                yield rotated


def _iter_skill_log_dirs(user_data: UserDataManager, *, agent_name: str):
    """Yield global and per-agent skill log directories."""
    seen: set[Path] = set()

    def _add(path: Path):
        if path.exists() and path.is_dir() and path not in seen:
            seen.add(path)
            return path
        return None

    if agent_name:
        path = _add(user_data.get_agent_skill_logs_dir(agent_name))
        if path is not None:
            yield path
        return

    path = _add(user_data.skill_logs_dir)
    if path is not None:
        yield path

    for agent in user_data.list_agents():
        path = _add(user_data.get_agent_skill_logs_dir(agent))
        if path is not None:
            yield path


def _sanitize_text_file(path: Path, *, summary: CleanupSummary, dry_run: bool) -> None:
    """Rewrite text log files in place after secret redaction."""
    summary.files_scanned += 1
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return

    lines_redacted = 0
    rewritten_lines: list[str] = []
    changed = False
    for line in original.splitlines(keepends=True):
        redacted = redact_sensitive_text(line)
        if redacted != line:
            changed = True
            lines_redacted += 1
        rewritten_lines.append(redacted)

    if not changed:
        return

    summary.files_updated += 1
    summary.lines_redacted += lines_redacted
    if dry_run:
        return

    _backup_file(path, summary)
    path.write_text("".join(rewritten_lines), encoding="utf-8")


def _migrate_skill_log_dir(logs_dir: Path, *, summary: CleanupSummary, dry_run: bool) -> None:
    """Normalize legacy skill JSONL files and persist oversized outputs as artifacts."""
    logger = SegmentLogger(logs_dir)
    for path in sorted(logs_dir.glob("*.jsonl")):
        summary.files_scanned += 1
        changed = False
        migrated_segments: list[EvaluationSegment] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                migrated_segments.append(
                    EvaluationSegment(
                        skill_id=path.stem,
                        skill_version="baseline",
                        triggered_at=datetime.now().isoformat(),
                        context_before="",
                        skill_output=redact_sensitive_text(stripped),
                        context_after="",
                        session_id="legacy",
                        status="invalid",
                        output_kind="raw_line",
                        error="malformed_jsonl_segment",
                    )
                )
                changed = True
                summary.skill_segments_migrated += 1
                continue

            normalized = _normalize_skill_segment(data, default_skill_id=path.stem)
            if normalized != data:
                changed = True
                summary.skill_segments_migrated += 1
            migrated_segments.append(EvaluationSegment.from_dict(normalized))

        if not changed:
            continue

        summary.files_updated += 1
        if dry_run:
            continue

        _backup_file(path, summary)
        for segment in migrated_segments:
            logger.append(segment)


def _normalize_skill_segment(data: dict, *, default_skill_id: str) -> dict:
    """Normalize one legacy skill segment into the current schema."""
    normalized = dict(data)
    normalized.setdefault("skill_id", default_skill_id)
    normalized.setdefault("skill_version", "baseline")
    normalized.setdefault("triggered_at", datetime.now().isoformat())
    normalized.setdefault("context_before", "")
    normalized.setdefault("context_after", "")
    normalized.setdefault("session_id", "legacy")
    normalized.setdefault("status", "completed")
    normalized.setdefault("output_kind", "final")
    normalized.setdefault("error", "")
    normalized.setdefault("output_truncated", False)
    normalized.setdefault("raw_output_path", "")

    output = str(normalized.get("skill_output", ""))
    normalized["skill_output"] = SkillLogRecorder._normalize_skill_output(
        redact_sensitive_text(output)
    )
    normalized["context_before"] = redact_sensitive_text(str(normalized.get("context_before", "")))
    normalized["context_after"] = redact_sensitive_text(str(normalized.get("context_after", "")))
    normalized["error"] = redact_sensitive_text(str(normalized.get("error", "")))
    return normalized


def _backup_file(path: Path, summary: CleanupSummary) -> Path:
    """Create a timestamped backup next to the original file."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.bak_{timestamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak_{timestamp}_{counter}")
        counter += 1
    path.replace(backup)
    summary.backups_created += 1
    return backup
