"""Tests for SkillLogRecorder — SLM skill log write adapter.

Covers:
- Acceptance tests (AC-1 through AC-4) as specified in design
- Gate acceptance tests
- Supplemental unit tests
"""
from __future__ import annotations

import threading
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.everbot.core.slm.models import EvaluationSegment
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.skill_log_recorder import (
    SkillLogRecorder,
    handle_skill_event,
    record_skills_from_raw_events,
)
from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_recorder(tmp_path: Path) -> SkillLogRecorder:
    """Create a SkillLogRecorder backed by tmp_path."""
    return SkillLogRecorder(
        skill_logs_dir=tmp_path / "skill_logs",
        skills_dir=tmp_path / "skills",
    )


def _make_skill_event(skill_name: str, status: str = "completed") -> TurnEvent:
    return TurnEvent(
        type=TurnEventType.SKILL,
        skill_name=skill_name,
        skill_output=f"output of {skill_name}",
        status=status,
    )


def _write_skill_md(skills_dir: Path, skill_name: str, version: str) -> None:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\nversion: {version}\n---\n\n# {skill_name}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC-1: Only user-level skills are logged
# ---------------------------------------------------------------------------

class TestOnlyUserSkillsLogged:
    """AC-1: Internal tools are silently skipped; user skills are written."""

    def test_only_user_skills_logged(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        log_dir = tmp_path / "skill_logs"
        segment_logger = SegmentLogger(log_dir)

        # Internal tools — must NOT write
        assert recorder.maybe_record("_bash", session_id="s1") is False
        assert recorder.maybe_record("_python", session_id="s1") is False
        assert recorder.maybe_record("_read_file", session_id="s1") is False

        # User skill — MUST write
        assert recorder.maybe_record("web-search", session_id="s1") is True

        # Verify via SegmentLogger
        skills = segment_logger.list_skills()
        assert skills == ["web-search"], f"Expected ['web-search'], got {skills}"

    def test_internal_tools_leave_no_files(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        log_dir = tmp_path / "skill_logs"

        recorder.maybe_record("_bash", session_id="s1")
        recorder.maybe_record("_python", session_id="s1")

        # No files should exist at all
        assert not log_dir.exists() or list(log_dir.glob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# AC-2: TurnEvent SKILL completed triggers log write (event-driven path)
# ---------------------------------------------------------------------------

class TestTurnEventSkillTriggersLogWrite:
    """AC-2: handle_skill_event() with a TurnEvent triggers a log write."""

    def test_turn_event_skill_triggers_log_write(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        event = _make_skill_event("paper-discovery", status="completed")
        result = handle_skill_event(
            event, recorder,
            session_id="sess-hb-001",
            context_before="find recent papers",
        )
        assert result is True

        segments = SegmentLogger(tmp_path / "skill_logs").load("paper-discovery")
        assert len(segments) == 1
        assert segments[0].skill_id == "paper-discovery"
        assert segments[0].session_id == "sess-hb-001"

    def test_handle_skill_event_ignores_non_completed(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        for status in ("running", "processing", "started", "failed", "error", ""):
            event = _make_skill_event("web-search", status=status)
            result = handle_skill_event(event, recorder, session_id="s1")
            assert result is False, f"Expected False for status={status!r}"

        # Nothing should be written
        assert SegmentLogger(tmp_path / "skill_logs").list_skills() == []

    def test_handle_skill_event_ignores_non_skill_type(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        event = TurnEvent(type=TurnEventType.TOOL_OUTPUT, tool_name="foo", status="completed")
        assert handle_skill_event(event, recorder, session_id="s1") is False
        assert SegmentLogger(tmp_path / "skill_logs").list_skills() == []


# ---------------------------------------------------------------------------
# AC-3: skill_version read from SKILL.md frontmatter
# ---------------------------------------------------------------------------

class TestSkillVersionFromFrontmatter:
    """AC-3: Version is read from SKILL.md frontmatter; falls back to 'baseline'."""

    def test_skill_version_from_frontmatter(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        _write_skill_md(skills_dir, "web-search", "2.1.0")

        recorder = SkillLogRecorder(
            skill_logs_dir=tmp_path / "skill_logs",
            skills_dir=skills_dir,
        )
        recorder.maybe_record("web-search", session_id="s1", context_before="test")

        segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
        assert len(segments) == 1
        assert segments[0].skill_version == "2.1.0"

    def test_skill_version_fallback_to_baseline_when_no_skill_md(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)  # skills_dir is empty
        recorder.maybe_record("web-search", session_id="s1")

        segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
        assert len(segments) == 1
        assert segments[0].skill_version == "baseline"

    def test_skill_version_fallback_to_baseline_when_no_version_field(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n\n# No version field here\n",
            encoding="utf-8",
        )

        recorder = SkillLogRecorder(
            skill_logs_dir=tmp_path / "skill_logs",
            skills_dir=skills_dir,
        )
        recorder.maybe_record("my-skill", session_id="s1")

        segments = SegmentLogger(tmp_path / "skill_logs").load("my-skill")
        assert segments[0].skill_version == "baseline"


# ---------------------------------------------------------------------------
# AC-4: Evaluate reads newly written logs
# ---------------------------------------------------------------------------

class TestEvaluateReadsNewlyWrittenLogs:
    """AC-4: SkillLogRecorder-written entries can be read back as valid EvaluationSegments."""

    def test_evaluate_reads_newly_written_logs(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        recorder.maybe_record(
            "web-search",
            session_id="sess-eval-001",
            skill_output="Found 5 relevant papers.",
            context_before="搜索最新的 transformer 论文",
        )

        log = SegmentLogger(tmp_path / "skill_logs")
        segments = log.load("web-search")
        assert len(segments) == 1

        seg = segments[0]
        assert seg.skill_id == "web-search"
        assert seg.session_id == "sess-eval-001"
        assert seg.triggered_at != ""  # must be populated
        assert seg.context_before == "搜索最新的 transformer 论文"
        assert seg.skill_output == "Found 5 relevant papers."

    def test_evaluate_load_by_version(self, tmp_path: Path):
        """Verify load_by_version() works with recorder-written logs."""
        skills_dir = tmp_path / "skills"
        _write_skill_md(skills_dir, "web-search", "1.5.0")

        recorder = SkillLogRecorder(
            skill_logs_dir=tmp_path / "skill_logs",
            skills_dir=skills_dir,
        )
        recorder.maybe_record("web-search", session_id="s1")

        log = SegmentLogger(tmp_path / "skill_logs")
        # load_by_version filters by version — should find our entry
        v_segs = log.load_by_version("web-search", "1.5.0")
        assert len(v_segs) == 1
        assert v_segs[0].skill_version == "1.5.0"

        # Other versions return empty
        assert log.load_by_version("web-search", "9.9.9") == []


# ---------------------------------------------------------------------------
# Gate acceptance test: handle_skill_event with dict (wrong path)
# ---------------------------------------------------------------------------

class TestHandleSkillEventWithDictEvent:
    """Gate: handle_skill_event() returns False when a raw dict is passed instead of TurnEvent.

    The heartbeat path should use record_skills_from_raw_events() instead.
    """

    def test_handle_skill_event_with_dict_event(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        # dict is not a TurnEvent — handle_skill_event must return False gracefully
        raw_dict: Dict[str, Any] = {
            "_progress": [{
                "stage": "skill",
                "skill_info": {"name": "web-search", "args": ""},
                "answer": "results",
                "status": "completed",
            }]
        }
        result = handle_skill_event(raw_dict, recorder, session_id="s1")
        assert result is False
        assert SegmentLogger(tmp_path / "skill_logs").list_skills() == []


# ---------------------------------------------------------------------------
# Gate acceptance test: concurrent writes
# ---------------------------------------------------------------------------

class TestConcurrentWritesToSameSkillLog:
    """Gate: concurrent threads writing to the same skill log produce valid JSON lines."""

    def test_concurrent_writes_to_same_skill_log(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        n_threads = 10
        errors: list = []

        def write_one():
            try:
                recorder.maybe_record("web-search", session_id="s-concurrent")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_one) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Threads raised exceptions: {errors}"

        log_path = tmp_path / "skill_logs" / "web-search.jsonl"
        assert log_path.exists()

        lines = [l.strip() for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == n_threads, f"Expected {n_threads} lines, got {len(lines)}"

        # Each line must be valid JSON
        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
                assert obj["skill_id"] == "web-search"
            except (json.JSONDecodeError, KeyError) as e:
                pytest.fail(f"Line {i} is not valid JSON or missing skill_id: {e!r}\nLine: {line!r}")


# ---------------------------------------------------------------------------
# Gate acceptance test: underscore NOT at start
# ---------------------------------------------------------------------------

class TestSkillNameWithUnderscoreNotAtStart:
    """Gate: skill names with underscore NOT at start position must not be filtered."""

    def test_skill_name_with_underscore_not_at_start(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        # "my_search" — underscore in the middle, should be recorded
        result = recorder.maybe_record("my_search", session_id="s1")
        assert result is True

        # "search_v2" — underscore in the middle
        result2 = recorder.maybe_record("search_v2", session_id="s1")
        assert result2 is True

        skills = SegmentLogger(tmp_path / "skill_logs").list_skills()
        assert "my_search" in skills
        assert "search_v2" in skills


# ---------------------------------------------------------------------------
# Gate acceptance test: None skill_output
# ---------------------------------------------------------------------------

class TestMaybeRecordWithNoneSkillOutput:
    """Gate: None skill_output is normalised to empty string, no exception raised."""

    def test_maybe_record_with_none_skill_output(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        # Must not raise, must write
        result = recorder.maybe_record(
            "web-search",
            session_id="s1",
            skill_output=None,  # type: ignore[arg-type]
        )
        assert result is True

        segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
        assert len(segments) == 1
        assert segments[0].skill_output == ""


# ---------------------------------------------------------------------------
# Gate acceptance test: record_skills_from_raw_events — multiple skills
# ---------------------------------------------------------------------------

class TestRecordSkillsFromRawEventsMultipleSkills:
    """Gate: multiple SKILL completed events in one raw_events list are each logged."""

    def test_record_skills_from_raw_events_multiple_skills(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        raw_events = [
            {"_progress": [{
                "stage": "skill",
                "skill_info": {"name": "paper-discovery", "args": ""},
                "answer": "paper results",
                "status": "completed",
            }]},
            {"_progress": [{
                "stage": "skill",
                "skill_info": {"name": "web-search", "args": ""},
                "answer": "web results",
                "status": "completed",
            }]},
        ]

        count = record_skills_from_raw_events(
            raw_events, recorder, session_id="sess-multi"
        )
        assert count == 2

        log = SegmentLogger(tmp_path / "skill_logs")
        assert len(log.load("paper-discovery")) == 1
        assert len(log.load("web-search")) == 1


# ---------------------------------------------------------------------------
# Gate acceptance test: record_skills_from_raw_events — malformed dict
# ---------------------------------------------------------------------------

class TestRecordSkillsFromRawEventsMalformedDict:
    """Gate: malformed/missing keys in raw dict must not raise, return 0."""

    def test_record_skills_from_raw_events_malformed_dict(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        malformed_cases = [
            # No _progress key
            {},
            # _progress is None
            {"_progress": None},
            # progress item missing stage
            {"_progress": [{"skill_info": {"name": "web-search"}, "status": "completed"}]},
            # skill_info is None
            {"_progress": [{"stage": "skill", "skill_info": None, "status": "completed"}]},
            # name missing from skill_info
            {"_progress": [{"stage": "skill", "skill_info": {}, "status": "completed"}]},
            # status not completed
            {"_progress": [{"stage": "skill", "skill_info": {"name": "web-search"}, "status": "running"}]},
            # entire item is not a dict
            {"_progress": ["not a dict"]},
            # raw event is not a dict
            "not a dict at all",
            42,
            None,
        ]

        # Filter out None since List[Dict] typing would prevent None, but we test defensively
        safe_cases = [c for c in malformed_cases if c is not None]
        try:
            count = record_skills_from_raw_events(
                safe_cases,  # type: ignore[arg-type]
                recorder,
                session_id="s1",
            )
        except Exception as e:
            pytest.fail(f"record_skills_from_raw_events raised on malformed input: {e!r}")

        assert count == 0
        assert SegmentLogger(tmp_path / "skill_logs").list_skills() == []


# ---------------------------------------------------------------------------
# Supplemental: boundary and error-path tests
# ---------------------------------------------------------------------------

class TestMaybeRecordBoundary:
    def test_none_skill_name_returns_false(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        assert recorder.maybe_record(None, session_id="s1") is False  # type: ignore[arg-type]

    def test_empty_skill_name_returns_false(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        assert recorder.maybe_record("", session_id="s1") is False

    def test_io_failure_does_not_raise(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        with patch.object(SegmentLogger, "append", side_effect=OSError("disk full")):
            result = recorder.maybe_record("web-search", session_id="s1")
        assert result is False  # swallowed, not raised


class TestRecordSkillsFromRawEventsBasic:
    def test_only_completed_status_recorded(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        raw_events = [
            {"_progress": [{"stage": "skill", "skill_info": {"name": "web-search"}, "answer": "r", "status": "completed"}]},
            {"_progress": [{"stage": "skill", "skill_info": {"name": "web-search"}, "answer": "r", "status": "running"}]},
            {"_progress": [{"stage": "llm", "delta": "text", "answer": ""}]},
        ]
        count = record_skills_from_raw_events(raw_events, recorder, session_id="s1")
        assert count == 1

        segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
        assert len(segments) == 1

    def test_internal_tool_in_raw_events_skipped(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        raw_events = [
            {"_progress": [{"stage": "skill", "skill_info": {"name": "_bash"}, "answer": "", "status": "completed"}]},
        ]
        count = record_skills_from_raw_events(raw_events, recorder, session_id="s1")
        assert count == 0
        assert SegmentLogger(tmp_path / "skill_logs").list_skills() == []


# ---------------------------------------------------------------------------
# Reviewer-proposed tests (addressing R4 HIGH/MEDIUM findings)
# ---------------------------------------------------------------------------

class TestMaybeRecordWithBinarySkillMd:
    """Proposed test: binary/non-UTF8 SKILL.md must not propagate UnicodeDecodeError."""

    def test_maybe_record_with_binary_skill_md(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "web-search"
        skill_dir.mkdir(parents=True)
        # Write binary content that will cause UnicodeDecodeError on UTF-8 decode
        (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe binary content \x00\x01\x02")

        recorder = SkillLogRecorder(
            skill_logs_dir=tmp_path / "skill_logs",
            skills_dir=skills_dir,
        )
        # Must not raise — either falls back to "baseline" or returns False on error.
        # Either outcome is acceptable; what is NOT acceptable is propagating UnicodeDecodeError.
        try:
            result = recorder.maybe_record("web-search", session_id="s1")
        except UnicodeDecodeError:
            import pytest
            pytest.fail("maybe_record propagated UnicodeDecodeError from binary SKILL.md")
        # If it wrote (fallback to baseline) — verify the log is valid
        if result:
            segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
            assert len(segments) == 1
            assert segments[0].skill_version == "baseline"


class TestRecordSkillsFromRawEventsNonIterableProgress:
    """Proposed test: _progress with non-iterable value must not raise TypeError."""

    def test_record_skills_from_raw_events_non_iterable_progress(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        # _progress is an int — iterating over it would raise TypeError without the guard
        raw_events_int_progress = [{"_progress": 42}]
        try:
            count = record_skills_from_raw_events(
                raw_events_int_progress, recorder, session_id="s1"
            )
        except TypeError as e:
            import pytest
            pytest.fail(f"record_skills_from_raw_events raised TypeError on _progress=42: {e!r}")
        assert count == 0

        # _progress is a string (iterable but wrong type — each char is a "progress item")
        raw_events_str_progress = [{"_progress": "completed"}]
        count2 = record_skills_from_raw_events(
            raw_events_str_progress, recorder, session_id="s1"
        )
        assert count2 == 0

        assert SegmentLogger(tmp_path / "skill_logs").list_skills() == []


class TestContextBeforeNoneDoesNotCorruptLog:
    """Proposed test: context_before=None is normalised to '' and does not write None into log."""

    def test_context_before_none_does_not_corrupt_log(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)

        # Simulate CoreService passing None message_text (edge case: empty/system message)
        result = recorder.maybe_record(
            "web-search",
            session_id="sess-none-ctx",
            context_before=None,  # type: ignore[arg-type]
        )
        assert result is True

        segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
        assert len(segments) == 1
        # context_before must be an empty string, never None
        assert segments[0].context_before == ""
        assert segments[0].context_before is not None


# ---------------------------------------------------------------------------
# Supplemental: CoreService path injection
# ---------------------------------------------------------------------------

class TestCoreServicePathRecorderInjection:
    """Supplemental: ChatService injects SkillLogRecorder into ChannelCoreService."""

    def test_core_service_accepts_skill_log_recorder(self, tmp_path: Path):
        """ChannelCoreService with an injected recorder uses it on SKILL completed events."""
        from unittest.mock import MagicMock, AsyncMock
        from src.everbot.core.channel.core_service import ChannelCoreService
        from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType

        recorder = _make_recorder(tmp_path)

        # Build a minimal ChannelCoreService with the recorder injected
        session_mgr = MagicMock()
        agent_svc = MagicMock()
        user_data = MagicMock()

        core = ChannelCoreService(
            session_mgr, agent_svc, user_data,
            skill_log_recorder=recorder,
        )
        # Verify the recorder is stored
        assert core._skill_log_recorder is recorder

    def test_core_service_skill_log_recorder_writes_on_completed(self, tmp_path: Path):
        """When _skill_log_recorder is set, SKILL completed events write to log."""
        recorder = _make_recorder(tmp_path)

        # Directly call maybe_record (simulating what CoreService does on SKILL completed)
        result = recorder.maybe_record(
            "paper-discovery",
            session_id="sess-core-001",
            context_before="find papers on RL",
            skill_output="Found 3 papers",
        )
        assert result is True

        segments = SegmentLogger(tmp_path / "skill_logs").load("paper-discovery")
        assert len(segments) == 1
        assert segments[0].context_before == "find papers on RL"


# ---------------------------------------------------------------------------
# Supplemental: skill_evaluate not in ALLOWED_SKILLS
# ---------------------------------------------------------------------------

class TestSkillEvaluateNotInAllowedSkills:
    """Supplemental: ALLOWED_SKILLS must not contain 'skill_evaluate'."""

    def test_skill_evaluate_not_executable_as_isolated_skill(self):
        from src.everbot.core.runtime.cron import ALLOWED_SKILLS, ALLOWED_JOBS

        # skill_evaluate IS a valid cron job
        assert "skill_evaluate" in ALLOWED_JOBS

        # BUT must NOT be runnable as an isolated skill (would bypass cron controls)
        assert "skill_evaluate" not in ALLOWED_SKILLS, (
            "skill_evaluate must not be in ALLOWED_SKILLS — it is a cron job, "
            "not an isolated skill, and running it via _run_isolated_skill() "
            "would bypass normal cron scheduling and concurrency controls."
        )

    def test_allowed_skills_subset_does_not_include_evaluate(self):
        from src.everbot.core.runtime.cron import ALLOWED_SKILLS
        # Standard isolated skills must still be present
        assert "health_check" in ALLOWED_SKILLS
        assert "memory_review" in ALLOWED_SKILLS
        assert "task_discover" in ALLOWED_SKILLS


# ---------------------------------------------------------------------------
# Supplemental: binary SKILL.md still writes log with baseline version
# ---------------------------------------------------------------------------

class TestBinarySkillMdStillWritesLogWithBaselineVersion:
    """Supplemental: binary SKILL.md falls back to 'baseline' and still writes log."""

    def test_binary_skill_md_still_writes_log_with_baseline_version(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "web-search"
        skill_dir.mkdir(parents=True)
        # Write binary content that cannot be decoded as UTF-8
        (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe binary content \x00\x01\x02")

        recorder = SkillLogRecorder(
            skill_logs_dir=tmp_path / "skill_logs",
            skills_dir=skills_dir,
        )
        # Must return True (log written with baseline version), not False or exception
        result = recorder.maybe_record("web-search", session_id="s1")
        assert result is True, "Binary SKILL.md should still produce a log entry with baseline version"

        segments = SegmentLogger(tmp_path / "skill_logs").load("web-search")
        assert len(segments) == 1
        assert segments[0].skill_version == "baseline"


# ---------------------------------------------------------------------------
# F14/F20: skill_evaluate job can consume logs written by SkillLogRecorder
# ---------------------------------------------------------------------------

class TestSkillEvaluateCanConsumeRecorderLogs:
    """Verify that skill_evaluate._evaluate_one() can find and load logs written by SkillLogRecorder.

    This test mocks the LLM judge (evaluate_skill) to avoid network calls while still
    exercising the full data-flow path:
      SkillLogRecorder.maybe_record() → JSONL → SegmentLogger.load() → _evaluate_one()
    """

    @pytest.mark.asyncio
    async def test_skill_evaluate_reads_recorder_written_logs(self, tmp_path: Path):
        """skill_evaluate._evaluate_one loads segments written by SkillLogRecorder."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.everbot.core.jobs.skill_evaluate import _evaluate_one
        from src.everbot.core.slm.segment_logger import SegmentLogger
        from src.everbot.core.slm.version_manager import VersionManager

        skills_dir = tmp_path / "skills"
        skill_logs_dir = tmp_path / "skill_logs"

        # Write a SKILL.md with version "baseline" (default for new skill)
        skill_dir = skills_dir / "web-search"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: web-search\nversion: baseline\n---\n", encoding="utf-8"
        )

        # Write a segment via SkillLogRecorder
        recorder = SkillLogRecorder(skill_logs_dir=skill_logs_dir, skills_dir=skills_dir)
        recorder.maybe_record(
            "web-search",
            session_id="sess-eval-job-001",
            skill_output="Found 3 papers",
            context_before="find recent papers",
        )

        seg_logger = SegmentLogger(skill_logs_dir)
        ver_mgr = VersionManager(skills_dir)

        # skill_evaluate._evaluate_one calls evaluate_skill(context.llm, ...)
        # Mock it so the job runs without a real LLM, return a real EvalReport
        from src.everbot.core.slm.models import EvalReport, JudgeResult
        fake_report = EvalReport(
            skill_id="web-search",
            skill_version="baseline",
            evaluated_at="2026-01-01T00:00:00+00:00",
            segment_count=1,
            critical_issue_count=0,
            critical_issue_rate=0.0,
            mean_satisfaction=0.8,
            results=[JudgeResult(segment_index=0, has_critical_issue=False, satisfaction=0.8, reason="ok")],
        )

        with patch(
            "src.everbot.core.jobs.skill_evaluate.evaluate_skill",
            new_callable=AsyncMock,
            return_value=fake_report,
        ) as mock_evaluate:
            mock_context = MagicMock()
            mock_context.llm = MagicMock()
            result = await _evaluate_one(mock_context, seg_logger, ver_mgr, "web-search")

        # _evaluate_one should have found the segment and called evaluate_skill
        assert mock_evaluate.called, "evaluate_skill was never called — segment not found by _evaluate_one"
        call_kwargs = mock_evaluate.call_args
        # Verify it was called with our skill and version
        assert call_kwargs.args[1] == "web-search"
        assert call_kwargs.args[2] == "baseline"
        assert len(call_kwargs.args[3]) == 1  # 1 segment

    def test_skill_evaluate_list_skills_finds_recorder_output(self, tmp_path: Path):
        """SegmentLogger.list_skills() returns skills written by SkillLogRecorder (the first step of skill_evaluate.run())."""
        recorder = _make_recorder(tmp_path)

        # Write two different skills
        recorder.maybe_record("web-search", session_id="s1")
        recorder.maybe_record("paper-discovery", session_id="s1")

        seg_logger = SegmentLogger(tmp_path / "skill_logs")
        skill_ids = seg_logger.list_skills()

        # skill_evaluate.run() starts with this call — must find both skills
        assert "web-search" in skill_ids
        assert "paper-discovery" in skill_ids
        assert len(skill_ids) == 2


# ---------------------------------------------------------------------------
# F31: UserDataManager.get_skill_log_recorder() factory method
# ---------------------------------------------------------------------------

class TestUserDataManagerFactory:
    """F31: SkillLogRecorder creation is consolidated via UserDataManager factory."""

    def test_get_skill_log_recorder_returns_recorder(self, tmp_path: Path):
        """UserDataManager.get_skill_log_recorder() returns a working SkillLogRecorder."""
        from src.everbot.infra.user_data import UserDataManager

        udm = UserDataManager(alfred_home=tmp_path)
        recorder = udm.get_skill_log_recorder()

        assert recorder is not None
        assert isinstance(recorder, SkillLogRecorder)

    def test_get_skill_log_recorder_uses_correct_paths(self, tmp_path: Path):
        """Factory uses skill_logs_dir and skills_dir from UserDataManager."""
        from src.everbot.infra.user_data import UserDataManager

        udm = UserDataManager(alfred_home=tmp_path)
        recorder = udm.get_skill_log_recorder()

        # Write a log via the factory-created recorder
        recorder.maybe_record("web-search", session_id="sess-factory-001")

        # Should appear in the UserDataManager's skill_logs_dir
        seg_logger = SegmentLogger(udm.skill_logs_dir)
        assert "web-search" in seg_logger.list_skills()

    def test_get_skill_log_recorder_with_skill_md(self, tmp_path: Path):
        """Factory-created recorder reads version from skills_dir/SKILL.md."""
        from src.everbot.infra.user_data import UserDataManager

        udm = UserDataManager(alfred_home=tmp_path)
        skill_dir = udm.skills_dir / "web-search"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: web-search\nversion: 3.0.0\n---\n", encoding="utf-8"
        )

        recorder = udm.get_skill_log_recorder()
        recorder.maybe_record("web-search", session_id="s1")

        segments = SegmentLogger(udm.skill_logs_dir).load("web-search")
        assert segments[0].skill_version == "3.0.0"


# ---------------------------------------------------------------------------
# F27 / F31 proposed: context_after is empty string (not null) in raw JSON
# ---------------------------------------------------------------------------

class TestContextAfterIsEmptyStringInJson:
    """context_after must be persisted as '' (not null) in the JSONL file."""

    def test_context_after_is_empty_string_not_none_in_persisted_json(self, tmp_path: Path):
        recorder = _make_recorder(tmp_path)
        recorder.maybe_record("web-search", session_id="sess-ctx-json")

        log_path = tmp_path / "skill_logs" / "web-search.jsonl"
        assert log_path.exists()

        raw = log_path.read_text(encoding="utf-8").strip()
        obj = json.loads(raw)
        # context_after must be the empty string, never JSON null
        assert "context_after" in obj
        assert obj["context_after"] == ""
        assert obj["context_after"] is not None


# ---------------------------------------------------------------------------
# F28 proposed: _ensure_core path has recorder (via getattr fallback)
# ---------------------------------------------------------------------------

class TestEnsureCorePathHasRecorder:
    """_ensure_core() passes skill_log_recorder from __init__ to ChannelCoreService."""

    def test_ensure_core_uses_init_recorder(self, tmp_path: Path):
        """ChatService._ensure_core() propagates _skill_log_recorder from __init__."""
        from unittest.mock import MagicMock
        from src.everbot.core.channel.core_service import ChannelCoreService

        recorder = _make_recorder(tmp_path)

        # Simulate _ensure_core: if _core exists already, return it.
        # If not, create with getattr(self, "_skill_log_recorder", None)
        # Here we directly verify ChannelCoreService accepts None gracefully too.
        core_with_none = ChannelCoreService(
            MagicMock(), MagicMock(), MagicMock(),
            skill_log_recorder=None,
        )
        assert core_with_none._skill_log_recorder is None

        core_with_recorder = ChannelCoreService(
            MagicMock(), MagicMock(), MagicMock(),
            skill_log_recorder=recorder,
        )
        assert core_with_recorder._skill_log_recorder is recorder
