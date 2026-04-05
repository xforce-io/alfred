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
        context_before=kw.get("context_before", "user: help"),
        skill_output=kw.get("skill_output", "assistant: done"),
        context_after=kw.get("context_after", "user: thanks"),
        session_id=session_id,
    )


class TestSegmentLogger:
    def test_append_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment())
            logger.append(_make_segment(session_id="s2"))

            loaded = logger.load("test-skill")
            assert len(loaded) == 2
            assert loaded[0].session_id == "s1"
            assert loaded[1].session_id == "s2"

    def test_load_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            assert logger.load("nonexistent") == []

    def test_load_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment(
                context_before="user: fix bug",
                skill_output="here is fix",
                context_after="user: ok",
            ))
            loaded = logger.load("test-skill")
            assert loaded[0].context_before == "user: fix bug"
            assert loaded[0].skill_output == "here is fix"
            assert loaded[0].context_after == "user: ok"

    def test_large_output_is_truncated_to_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            large_output = "result:" + ("x" * 6000)

            logger.append(_make_segment(skill_output=large_output))

            loaded = logger.load("test-skill")
            assert len(loaded) == 1
            assert loaded[0].output_truncated is True
            assert loaded[0].raw_output_path
            assert Path(loaded[0].raw_output_path).exists()
            assert Path(loaded[0].raw_output_path).read_text(encoding="utf-8") == large_output
            assert loaded[0].skill_output != large_output
            assert loaded[0].skill_output.endswith("...[truncated]")

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

    def test_cleanup_max_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            import src.everbot.core.slm.segment_logger as mod

            orig = mod._MAX_ENTRIES
            try:
                mod._MAX_ENTRIES = 3
                for i in range(5):
                    logger.append(_make_segment(session_id=f"s{i}"))

                removed = logger.cleanup("test-skill")
                assert removed == 2
                remaining = logger.load("test-skill")
                assert len(remaining) == 3
                # Should keep most recent
                assert remaining[0].session_id == "s2"
            finally:
                mod._MAX_ENTRIES = orig


class TestBackfillContextAfter:
    def test_backfill_basic(self):
        """context_after is empty at write time and filled by backfill."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment(session_id="sess1", context_after=""))

            patched = logger.backfill_context_after(
                "test-skill", "sess1", "user: that worked",
            )
            assert patched is True

            loaded = logger.load("test-skill")
            assert loaded[0].context_after == "user: that worked"

    def test_backfill_no_match(self):
        """backfill returns False when no matching session_id exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment(session_id="other", context_after=""))

            patched = logger.backfill_context_after(
                "test-skill", "nonexistent", "user: hi",
            )
            assert patched is False

    def test_backfill_skips_already_filled(self):
        """backfill does not overwrite segments that already have context_after."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment(session_id="s1", context_after="already set"))

            patched = logger.backfill_context_after(
                "test-skill", "s1", "new value",
            )
            assert patched is False

            loaded = logger.load("test-skill")
            assert loaded[0].context_after == "already set"

    def test_backfill_targets_last_matching(self):
        """Only the most recent empty segment for the session is patched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            logger.append(_make_segment(session_id="s1", context_after=""))
            logger.append(_make_segment(session_id="s1", context_after=""))

            patched = logger.backfill_context_after(
                "test-skill", "s1", "user: thanks",
            )
            assert patched is True

            loaded = logger.load("test-skill")
            assert loaded[0].context_after == ""  # first untouched
            assert loaded[1].context_after == "user: thanks"  # last patched

    def test_backfill_nonexistent_file(self):
        """backfill returns False for a skill with no log file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SegmentLogger(Path(tmpdir))
            assert logger.backfill_context_after("ghost", "s1", "hi") is False
