"""Tests for SLM SegmentLogger."""

import json
import tempfile
from pathlib import Path

from src.everbot.core.slm.models import SkillLogEntry
from src.everbot.core.slm.segment_logger import SegmentLogger


def _make_entry(skill_id="test-skill", version="1.0", session_id="s1", **kw):
    return SkillLogEntry(
        skill_id=skill_id,
        skill_version=version,
        session_id=session_id,
        run_id=kw.get("run_id", "run_001"),
        triggered_at=kw.get("triggered_at", "2026-03-17T10:00:00Z"),
    )


def _make_session_file(sessions_dir: Path, session_id: str, run_id: str):
    """Create a minimal session JSON file for resolve tests."""
    session = {
        "session_id": session_id,
        "timeline": [
            {"type": "turn_start", "run_id": run_id, "timestamp": "2026-03-17T10:00:00"},
            {"type": "skill", "run_id": run_id, "skill_name": "test-skill", "status": "completed"},
            {"type": "turn_end", "run_id": run_id},
        ],
        "history_messages": [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "here is the fix"},
            {"role": "user", "content": "that worked"},
        ],
    }
    path = sessions_dir / f"web_session_{session_id}.json"
    path.write_text(json.dumps(session), encoding="utf-8")
    return path


class TestSegmentLogger:
    def test_append_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            entry = _make_entry()
            logger.append(entry)
            logger.append(_make_entry(session_id="s2"))

            loaded = logger.load("test-skill")
            assert len(loaded) == 2
            assert loaded[0].session_id == "s1"
            assert loaded[1].session_id == "s2"

    def test_load_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            assert logger.load("nonexistent") == []

    def test_load_by_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_entry(version="1.0"))
            logger.append(_make_entry(version="2.0"))
            logger.append(_make_entry(version="1.0"))

            v1 = logger.load_by_version("test-skill", "1.0")
            assert len(v1) == 2

            v2 = logger.load_by_version("test-skill", "2.0")
            assert len(v2) == 1

    def test_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            assert logger.count("test-skill") == 0
            logger.append(_make_entry())
            logger.append(_make_entry())
            assert logger.count("test-skill") == 2

    def test_list_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_entry(skill_id="alpha"))
            logger.append(_make_entry(skill_id="beta"))
            assert logger.list_skills() == ["alpha", "beta"]

    def test_cleanup_max_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            import src.everbot.core.slm.segment_logger as mod

            orig = mod._MAX_ENTRIES
            try:
                mod._MAX_ENTRIES = 3
                for i in range(5):
                    logger.append(_make_entry(session_id=f"s{i}"))

                removed = logger.cleanup("test-skill")
                assert removed == 2
                remaining = logger.load("test-skill")
                assert len(remaining) == 3
                # Should keep most recent
                assert remaining[0].session_id == "s2"
            finally:
                mod._MAX_ENTRIES = orig


class TestSegmentLoggerResolve:
    def test_resolve_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()

            logger = SegmentLogger(logs_dir)
            entry = _make_entry(session_id="sess1", run_id="run_abc")
            logger.append(entry)

            _make_session_file(sessions_dir, "sess1", "run_abc")

            entries = logger.load("test-skill")
            segments = logger.resolve(entries, sessions_dir)

            assert len(segments) == 1
            seg = segments[0]
            assert seg.skill_id == "test-skill"
            assert seg.context_before == "fix the bug"
            assert seg.skill_output == "here is the fix"
            assert seg.context_after == "that worked"

    def test_resolve_missing_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()

            logger = SegmentLogger(logs_dir)
            entry = _make_entry(session_id="gone", run_id="run_xyz")
            logger.append(entry)

            entries = logger.load("test-skill")
            segments = logger.resolve(entries, sessions_dir)
            assert len(segments) == 0
