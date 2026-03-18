"""Tests for SLM SegmentLogger."""

import tempfile
from pathlib import Path

from src.everbot.core.slm.models import EvaluationSegment
from src.everbot.core.slm.segment_logger import SegmentLogger


def _make_segment(skill_id="test-skill", version="1.0", session_id="s1", **kw):
    return EvaluationSegment(
        skill_id=skill_id,
        skill_version=version,
        triggered_at=kw.get("triggered_at", "2026-03-17T10:00:00Z"),
        context_before="user: hello",
        skill_output="assistant: hi",
        context_after="user: thanks",
        session_id=session_id,
    )


class TestSegmentLogger:
    def test_append_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            seg = _make_segment()
            logger.append(seg)
            logger.append(_make_segment(session_id="s2"))

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
            logger.append(_make_segment(version="1.0"))
            logger.append(_make_segment(version="2.0"))
            logger.append(_make_segment(version="1.0"))

            v1 = logger.load_by_version("test-skill", "1.0")
            assert len(v1) == 2

            v2 = logger.load_by_version("test-skill", "2.0")
            assert len(v2) == 1

    def test_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            assert logger.count("test-skill") == 0
            logger.append(_make_segment())
            logger.append(_make_segment())
            assert logger.count("test-skill") == 2

    def test_list_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment(skill_id="alpha"))
            logger.append(_make_segment(skill_id="beta"))
            assert logger.list_skills() == ["alpha", "beta"]

    def test_cleanup_max_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            # Write more than _MAX_SEGMENTS (monkey-patch for test)
            import src.everbot.core.slm.segment_logger as mod

            orig = mod._MAX_SEGMENTS
            try:
                mod._MAX_SEGMENTS = 3
                for i in range(5):
                    logger.append(_make_segment(session_id=f"s{i}"))

                removed = logger.cleanup("test-skill")
                assert removed == 2
                remaining = logger.load("test-skill")
                assert len(remaining) == 3
                # Should keep most recent
                assert remaining[0].session_id == "s2"
            finally:
                mod._MAX_SEGMENTS = orig
