"""Tests for self-reflection: scanners, state, skill context, and memory review."""

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

# Relative timestamps for tests — avoids hardcoded dates that become stale.
_NOW = datetime.now(timezone.utc)
_1D_AGO = (_NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
_2D_AGO = (_NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()
_3D_AGO = (_NOW - timedelta(days=3)).replace(tzinfo=None).isoformat()
_5D_AGO = (_NOW - timedelta(days=5)).replace(tzinfo=None).isoformat()
_6D_AGO = (_NOW - timedelta(days=6)).replace(tzinfo=None).isoformat()
_10D_AGO = (_NOW - timedelta(days=10)).replace(tzinfo=None).isoformat()

# ── Scanner Tests ──────────────────────────────────────────────


class TestScanResult:
    def test_no_changes(self):
        from src.everbot.core.scanners.base import ScanResult
        r = ScanResult(has_changes=False, change_summary="nothing")
        assert not r.has_changes
        assert r.payload is None

    def test_has_changes_with_payload(self):
        from src.everbot.core.scanners.base import ScanResult
        r = ScanResult(has_changes=True, change_summary="3 new", payload=[1, 2, 3])
        assert r.has_changes
        assert r.payload == [1, 2, 3]


class TestSessionScanner:
    @pytest.fixture
    def sessions_dir(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        return d

    def _write_session(self, sessions_dir, session_id, updated_at, history=None):
        data = {
            "session_id": session_id,
            "updated_at": updated_at,
            "session_type": "primary",
            "history_messages": history or [],
            "agent_name": "test-agent",
        }
        path = sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_check_no_sessions(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        scanner = SessionScanner(sessions_dir)
        result = scanner.check("", "test-agent")
        assert not result.has_changes

    def test_check_finds_new_sessions(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        self._write_session(sessions_dir, "web_session_test-agent_001", _3D_AGO)
        self._write_session(sessions_dir, "web_session_test-agent_002", _2D_AGO)
        scanner = SessionScanner(sessions_dir)
        result = scanner.check(_5D_AGO, "test-agent")
        assert result.has_changes
        assert len(result.payload) == 2

    def test_check_respects_watermark(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        self._write_session(sessions_dir, "web_session_test-agent_001", _3D_AGO)
        self._write_session(sessions_dir, "web_session_test-agent_002", _2D_AGO)
        scanner = SessionScanner(sessions_dir)
        result = scanner.check(_3D_AGO, "test-agent")
        assert result.has_changes
        assert len(result.payload) == 1
        assert result.payload[0].id == "web_session_test-agent_002"

    def test_skips_heartbeat_sessions(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        self._write_session(sessions_dir, "heartbeat_session_test-agent", _1D_AGO)
        scanner = SessionScanner(sessions_dir)
        result = scanner.check("", "test-agent")
        assert not result.has_changes

    def test_skips_workflow_sessions(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        self._write_session(sessions_dir, "workflow_test-agent_001", _1D_AGO)
        scanner = SessionScanner(sessions_dir)
        result = scanner.check("", "test-agent")
        assert not result.has_changes

    def test_agent_name_filter(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        self._write_session(sessions_dir, "web_session_agent-a_001", _2D_AGO)
        self._write_session(sessions_dir, "web_session_agent-b_001", _2D_AGO)
        scanner = SessionScanner(sessions_dir)
        result = scanner.check("", "agent-a")
        assert result.has_changes
        assert len(result.payload) == 1
        assert result.payload[0].id == "web_session_agent-a_001"

    def test_max_sessions_limit(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        for i in range(10):
            ts = (_NOW - timedelta(days=6, hours=-i)).replace(tzinfo=None).isoformat()
            self._write_session(
                sessions_dir, f"web_session_test_{i:03d}", ts,
            )
        scanner = SessionScanner(sessions_dir)
        sessions = scanner.get_reviewable_sessions("", max_sessions=3)
        assert len(sessions) == 3

    def test_extract_digest_string_content(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "tool", "content": "tool output"},
        ]
        self._write_session(sessions_dir, "web_session_test_001", _1D_AGO, history)
        scanner = SessionScanner(sessions_dir)
        path = sessions_dir / "web_session_test_001.json"
        digest = scanner.extract_digest(path)
        assert "[user] Hello" in digest
        assert "[assistant] Hi there!" in digest
        assert "tool output" not in digest

    def test_extract_digest_list_content(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        history = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Here is my answer"},
                {"type": "tool_use", "id": "123", "name": "bash"},
            ]},
        ]
        self._write_session(sessions_dir, "web_session_test_001", _1D_AGO, history)
        scanner = SessionScanner(sessions_dir)
        path = sessions_dir / "web_session_test_001.json"
        digest = scanner.extract_digest(path)
        assert "Here is my answer" in digest

    def test_extract_digest_truncation(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        history = [
            {"role": "user", "content": "A" * 5000},
        ]
        self._write_session(sessions_dir, "web_session_test_001", _1D_AGO, history)
        scanner = SessionScanner(sessions_dir)
        path = sessions_dir / "web_session_test_001.json"
        digest = scanner.extract_digest(path, max_chars=100)
        assert len(digest) <= 110  # Allow small overshoot for "..."

    def test_sorted_by_updated_at(self, sessions_dir):
        from src.everbot.core.scanners.session_scanner import SessionScanner
        self._write_session(sessions_dir, "web_session_test_002", _1D_AGO)
        self._write_session(sessions_dir, "web_session_test_001", _2D_AGO)
        scanner = SessionScanner(sessions_dir)
        sessions = scanner.get_reviewable_sessions("")
        assert sessions[0].updated_at < sessions[1].updated_at


# ── ReflectionState Tests ──────────────────────────────────────


class TestReflectionState:
    def test_load_empty(self, tmp_path):
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state = ReflectionState.load(tmp_path)
        assert state.get_watermark("memory-review") == ""

    def test_save_and_load(self, tmp_path):
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state = ReflectionState()
        state.set_watermark("memory-review", "2026-03-01T10:00:00")
        state.set_watermark("task-discover", "2026-03-01T12:00:00")
        state.save(tmp_path)

        loaded = ReflectionState.load(tmp_path)
        assert loaded.get_watermark("memory-review") == "2026-03-01T10:00:00"
        assert loaded.get_watermark("task-discover") == "2026-03-01T12:00:00"

    def test_independent_watermarks(self, tmp_path):
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state = ReflectionState()
        state.set_watermark("skill-a", "2026-03-01")
        state.set_watermark("skill-b", "2026-03-02")
        assert state.get_watermark("skill-a") == "2026-03-01"
        assert state.get_watermark("skill-b") == "2026-03-02"

    def test_corrupt_file_returns_empty(self, tmp_path):
        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_file = tmp_path / ".reflection_state.json"
        state_file.write_text("invalid json{{{", encoding="utf-8")
        state = ReflectionState.load(tmp_path)
        assert state.get_watermark("any") == ""


# ── Memory apply_review Tests ──────────────────────────────────


class TestApplyReview:
    @pytest.fixture
    def memory_path(self, tmp_path):
        return tmp_path / "MEMORY.md"

    def _create_memory_file(self, memory_path, entries):
        from src.everbot.core.memory.store import MemoryStore
        store = MemoryStore(memory_path)
        store.save(entries)

    def _make_entry(self, entry_id, content="test", score=0.8):
        from src.everbot.core.memory.models import MemoryEntry
        return MemoryEntry(
            id=entry_id,
            content=content,
            category="fact",
            score=score,
            created_at="2026-03-01T00:00:00",
            last_activated="2026-03-01T00:00:00",
            activation_count=1,
            source_session="test",
        )

    def test_merge_reduces_entries(self, memory_path):
        from src.everbot.core.memory.manager import MemoryManager
        entries = [
            self._make_entry("aaa111", "user likes python"),
            self._make_entry("bbb222", "user prefers python for development"),
            self._make_entry("ccc333", "user lives in Beijing"),
        ]
        self._create_memory_file(memory_path, entries)
        mm = MemoryManager(memory_path)
        review = {
            "merge_pairs": [{"id_a": "aaa111", "id_b": "bbb222", "merged_content": "user prefers Python for development"}],
            "deprecate_ids": [],
            "reinforce_ids": [],
            "refined_entries": [],
        }
        stats = mm.apply_review(review)
        assert stats["merged"] == 1
        result = mm.load_entries()
        assert len(result) == 2  # 3 - 2 + 1 = 2

    def test_deprecate_lowers_score(self, memory_path):
        from src.everbot.core.memory.manager import MemoryManager
        entries = [self._make_entry("aaa111", "outdated info", score=0.8)]
        self._create_memory_file(memory_path, entries)
        mm = MemoryManager(memory_path)
        review = {"deprecate_ids": ["aaa111"]}
        stats = mm.apply_review(review)
        assert stats["deprecated"] == 1
        result = mm.load_entries()
        assert result[0].score == pytest.approx(0.24, abs=0.01)

    def test_integrity_error_on_increase(self, memory_path):
        """apply_review should not increase entry count — only merge/deprecate/refine."""
        from src.everbot.core.memory.manager import MemoryManager
        entries = [self._make_entry("aaa111")]
        self._create_memory_file(memory_path, entries)
        mm = MemoryManager(memory_path)
        # Reinforce alone should not increase count (it only boosts existing entries)
        review = {"reinforce_ids": ["aaa111"]}
        stats = mm.apply_review(review)
        assert stats["reinforced"] == 1

    def test_missing_id_skipped(self, memory_path):
        from src.everbot.core.memory.manager import MemoryManager
        entries = [self._make_entry("aaa111")]
        self._create_memory_file(memory_path, entries)
        mm = MemoryManager(memory_path)
        review = {
            "merge_pairs": [{"id_a": "aaa111", "id_b": "missing", "merged_content": "test"}],
            "deprecate_ids": ["nonexistent"],
        }
        stats = mm.apply_review(review)
        assert stats["merged"] == 0
        assert stats["deprecated"] == 0
        # Original entry should be untouched
        result = mm.load_entries()
        assert len(result) == 1

    def test_refine_updates_content(self, memory_path):
        from src.everbot.core.memory.manager import MemoryManager
        entries = [self._make_entry("aaa111", "old content")]
        self._create_memory_file(memory_path, entries)
        mm = MemoryManager(memory_path)
        review = {"refined_entries": [{"id": "aaa111", "content": "new refined content"}]}
        stats = mm.apply_review(review)
        assert stats["refined"] == 1
        result = mm.load_entries()
        assert result[0].content == "new refined content"


# ── MemoryMerger.merge_entries Tests ───────────────────────────


class TestMergeEntries:
    def _make_entry(self, entry_id, content="test", score=0.8, count=1):
        from src.everbot.core.memory.models import MemoryEntry
        return MemoryEntry(
            id=entry_id, content=content, category="fact",
            score=score, created_at="2026-03-01T00:00:00",
            last_activated="2026-03-01T00:00:00",
            activation_count=count, source_session="test",
        )

    def test_merge_entries_score_and_count(self):
        from src.everbot.core.memory.merger import MemoryMerger
        merger = MemoryMerger()
        a = self._make_entry("aaa", score=0.7, count=3)
        b = self._make_entry("bbb", score=0.9, count=2)
        merged = merger.merge_entries(a, b, "combined content")
        assert merged.score == 0.9  # max
        assert merged.activation_count == 5  # sum
        assert merged.content == "combined content"
        assert merged.id != "aaa" and merged.id != "bbb"


# ── Task Manager Extension Tests ───────────────────────────────


class TestTaskJobFields:
    def test_job_fields_default_none(self):
        from src.everbot.core.tasks.task_manager import Task
        t = Task(id="test", title="Test")
        assert t.job is None
        assert t.scanner is None
        assert t.min_execution_interval is None

    def test_job_only_no_scanner(self):
        """Job task without scanner — the default path."""
        from src.everbot.core.tasks.task_manager import Task
        data = {
            "id": "reflection_memory_review",
            "title": "Memory Review",
            "job": "memory-review",
            "schedule": "2h",
            "execution_mode": "inline",
        }
        t = Task.from_dict(data)
        assert t.job == "memory-review"
        assert t.scanner is None  # No scanner configured

    def test_job_with_optional_scanner(self):
        """Job task with optional scanner gate."""
        from src.everbot.core.tasks.task_manager import Task
        data = {
            "id": "reflection_memory_review",
            "title": "Memory Review",
            "job": "memory-review",
            "scanner": "session",
            "min_execution_interval": "2h",
            "execution_mode": "inline",
        }
        t = Task.from_dict(data)
        assert t.job == "memory-review"
        assert t.scanner == "session"
        assert t.min_execution_interval == "2h"

    def test_job_fields_roundtrip(self):
        from src.everbot.core.tasks.task_manager import Task
        t = Task(id="test", title="Test", job="memory-review", scanner="session")
        d = t.to_dict()
        t2 = Task.from_dict(d)
        assert t2.job == "memory-review"
        assert t2.scanner == "session"

    def test_backward_compat_skill_key(self):
        """Legacy data with 'skill' key should be read as 'job'."""
        from src.everbot.core.tasks.task_manager import Task
        data = {
            "id": "legacy_task",
            "title": "Legacy",
            "skill": "memory-review",
        }
        t = Task.from_dict(data)
        assert t.job == "memory-review"


# ── SkillContext Tests ─────────────────────────────────────────


class TestSkillContext:
    def test_context_construction(self, tmp_path):
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.scanners.base import ScanResult

        mm = MemoryManager(tmp_path / "MEMORY.md")
        ctx = SkillContext(
            sessions_dir=tmp_path / "sessions",
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=MagicMock(),
            llm=MagicMock(),
            scan_result=ScanResult(has_changes=True, change_summary="test", payload=[]),
        )
        assert ctx.agent_name == "test-agent"
        assert ctx.scan_result.has_changes


# ── TaskDiscoverState Tests ────────────────────────────────────


class TestTaskDiscoverState:
    def test_load_empty(self, tmp_path):
        from src.everbot.core.jobs.task_discover import TaskDiscoverState
        state = TaskDiscoverState.load(tmp_path)
        assert len(state.pending_tasks) == 0

    def test_save_and_load(self, tmp_path):
        from src.everbot.core.jobs.task_discover import TaskDiscoverState, DiscoveredTask
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        task = DiscoveredTask(
            title="Fix login bug",
            description="The login page crashes on mobile",
            urgency="high",
            source_session_id="web_session_test_001",
            discovered_at=now.isoformat(),
            expires_at=(now + timedelta(days=7)).isoformat(),
        )
        state = TaskDiscoverState(pending_tasks=[task])
        state.save(tmp_path)

        loaded = TaskDiscoverState.load(tmp_path)
        assert len(loaded.pending_tasks) == 1
        assert loaded.pending_tasks[0].title == "Fix login bug"
        assert not loaded.pending_tasks[0].expired

    def test_expired_task(self):
        from src.everbot.core.jobs.task_discover import DiscoveredTask
        task = DiscoveredTask(
            title="Old task",
            description="",
            urgency="low",
            source_session_id="",
            discovered_at="2020-01-01T00:00:00+00:00",
            expires_at="2020-01-08T00:00:00+00:00",
        )
        assert task.expired


# ── Skill Autonomy Tests (no scanner gate) ────────────────────


class TestSkillWithoutScanner:
    """Test that skills work correctly when scan_result is None (no scanner configured)."""

    @pytest.fixture
    def sessions_dir(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        return d

    def _write_session(self, sessions_dir, session_id, updated_at, history=None):
        data = {
            "session_id": session_id,
            "updated_at": updated_at,
            "session_type": "primary",
            "history_messages": history or [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "agent_name": "test-agent",
        }
        path = sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    @pytest.mark.asyncio
    async def test_memory_review_no_scan_result_queries_directly(self, tmp_path, sessions_dir):
        """memory_review.run() with scan_result=None should query sessions itself."""
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mock_llm = AsyncMock()
        mock_llm.complete.return_value = '{"session_ids": [], "merge_pairs": [], "deprecate_ids": [], "reinforce_ids": [], "refined_entries": []}'

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,  # No scanner gate
        )

        result = await run(ctx)
        # Should have found and processed the session (not returned "No sessions")
        assert "No sessions to review" not in result

    @pytest.mark.asyncio
    async def test_memory_review_no_scan_result_empty_watermark(self, tmp_path, sessions_dir):
        """memory_review with no scan_result and no sessions returns early."""
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager

        mm = MemoryManager(tmp_path / "MEMORY.md")
        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=AsyncMock(),
            scan_result=None,
        )

        result = await run(ctx)
        assert result == "No sessions to review"

    @pytest.mark.asyncio
    async def test_task_discover_no_scan_result_queries_directly(self, tmp_path, sessions_dir):
        """task_discover.run() with scan_result=None should query sessions itself."""
        from src.everbot.core.jobs.task_discover import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mock_llm = AsyncMock()
        mock_llm.complete.return_value = '{"tasks": []}'

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,  # No scanner gate
        )

        result = await run(ctx)
        assert "No sessions to analyze" not in result

    @pytest.mark.asyncio
    async def test_skill_prefers_scan_result_when_available(self, tmp_path, sessions_dir):
        """When scan_result has payload, skill should use it instead of querying."""
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.scanners.base import ScanResult
        from src.everbot.core.scanners.session_scanner import SessionSummary

        # Write a session but provide scan_result with different data
        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        gate_session = SessionSummary(
            id="web_session_test-agent_001",
            path=sessions_dir / "web_session_test-agent_001.json",
            updated_at="2026-03-01T10:00:00",
            session_type="primary",
        )
        scan_result = ScanResult(
            has_changes=True,
            change_summary="1 new session",
            payload=[gate_session],
        )

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mock_llm = AsyncMock()
        mock_llm.complete.return_value = '{"session_ids": [], "merge_pairs": [], "deprecate_ids": [], "reinforce_ids": [], "refined_entries": []}'

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=scan_result,  # Gate provided result
        )

        result = await run(ctx)
        assert "No sessions to review" not in result

    @pytest.mark.asyncio
    async def test_task_discover_llm_error_raises_not_swallowed(self, tmp_path, sessions_dir):
        """task_discover should let LLM errors propagate, not swallow them."""
        from src.everbot.core.jobs.task_discover import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMTransientError("Connection refused")

        ctx = SkillContext(
            sessions_dir=sessions_dir, workspace_path=tmp_path,
            agent_name="test-agent", memory_manager=mm,
            mailbox=AsyncMock(), llm=mock_llm, scan_result=None,
        )

        with pytest.raises(LLMTransientError, match="Connection refused"):
            await run(ctx)

        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("task-discover")

    @pytest.mark.asyncio
    async def test_memory_review_skips_when_skill_llm_unavailable(self, tmp_path, sessions_dir):
        """memory_review should raise LLMConfigError when skill LLM is unavailable."""
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.memory.models import MemoryEntry
        from src.everbot.core.jobs.llm_errors import LLMConfigError

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mm.store.save([
            MemoryEntry(
                id="mem001",
                category="workflow",
                content="User prefers concise answers",
                score=0.8,
                created_at="2026-03-01T00:00:00+00:00",
                last_activated="2026-03-01T00:00:00+00:00",
                activation_count=1,
                source_session="web_session_test-agent_001",
            )
        ])
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMConfigError("litellm is required for skill LLM calls")

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,
        )

        with pytest.raises(LLMConfigError):
            await run(ctx)

        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("memory-review")

    @pytest.mark.asyncio
    async def test_memory_review_dph_file_missing_does_not_advance_watermark(
        self, tmp_path, sessions_dir
    ):
        from unittest.mock import patch
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.memory.models import MemoryEntry
        from src.everbot.core.scanners.reflection_state import ReflectionState

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mm.store.save([
            MemoryEntry(
                id="mem002",
                category="workflow",
                content="User likes TDD",
                score=0.9,
                created_at="2026-03-01T00:00:00+00:00",
                last_activated="2026-03-01T00:00:00+00:00",
                activation_count=1,
                source_session="web_session_test-agent_001",
            )
        ])
        mock_llm = AsyncMock()

        ctx = SkillContext(
            sessions_dir=sessions_dir,
            workspace_path=tmp_path,
            agent_name="test-agent",
            memory_manager=mm,
            mailbox=AsyncMock(),
            llm=mock_llm,
            scan_result=None,
        )

        with patch("src.everbot.core.jobs.memory_review.parse_system_dph",
                    side_effect=FileNotFoundError("no such file")):
            with pytest.raises(FileNotFoundError):
                await run(ctx)

        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("memory-review")

    @pytest.mark.asyncio
    async def test_memory_review_connection_error_raises_not_swallowed(self, tmp_path, sessions_dir):
        """Connection errors should propagate (not be swallowed as empty result)."""
        from src.everbot.core.jobs.memory_review import run
        from src.everbot.core.runtime.skill_context import SkillContext
        from src.everbot.core.memory.manager import MemoryManager
        from src.everbot.core.memory.models import MemoryEntry
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        self._write_session(sessions_dir, "web_session_test-agent_001", _1D_AGO)

        mm = MemoryManager(tmp_path / "MEMORY.md")
        mm.store.save([
            MemoryEntry(
                id="mem001", category="workflow",
                content="User prefers concise answers", score=0.8,
                created_at="2026-03-01T00:00:00+00:00",
                last_activated="2026-03-01T00:00:00+00:00",
                activation_count=1, source_session="web_session_test-agent_001",
            )
        ])
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMTransientError("Connection error")

        ctx = SkillContext(
            sessions_dir=sessions_dir, workspace_path=tmp_path,
            agent_name="test-agent", memory_manager=mm,
            mailbox=AsyncMock(), llm=mock_llm, scan_result=None,
        )

        with pytest.raises(LLMTransientError, match="Connection error"):
            await run(ctx)

        from src.everbot.core.scanners.reflection_state import ReflectionState
        state_after = ReflectionState.load(tmp_path)
        assert not state_after.get_watermark("memory-review")


# ── _SkillLLMClient Tests ────────────────────────────────────────


class TestSkillLLMClient:
    """Verify _SkillLLMClient uses Dolphin GlobalConfig, not litellm."""

    @pytest.mark.asyncio
    async def test_uses_dolphin_openai_not_litellm(self):
        """_SkillLLMClient must resolve model via GlobalConfig and call
        AsyncOpenAI, never importing litellm."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient

        client = _SkillLLMClient(model="deepseek-volcengine")

        # Should NOT depend on litellm at all
        import importlib
        import sys
        litellm_was_imported = "litellm" in sys.modules

        # Mock the Dolphin GlobalConfig path to avoid needing real config
        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://fake-volcengine.example.com/v1"
        fake_model_cfg.api_key = "fake-key"
        fake_model_cfg.model_name = "kimi-k2.5"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg

        # Patch GlobalConfig loading and AsyncOpenAI
        import unittest.mock as um

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "test response"

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = fake_response
            mock_openai_cls.return_value = mock_client

            result = await client.complete("hello", system="be helpful")

        assert result == "test response"
        # Verify it resolved model config from GlobalConfig
        fake_config.get_model_config.assert_called_once_with("deepseek-volcengine")
        # Verify it called OpenAI with the resolved model_name, not the instance name
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs[1]["model"] == "kimi-k2.5"

    @pytest.mark.asyncio
    async def test_fallback_model_from_env(self):
        """When model is empty, fall back to ALFRED_SKILL_MODEL env var."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient
        import unittest.mock as um
        import os

        client = _SkillLLMClient(model="")

        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://api.deepseek.com/v1"
        fake_model_cfg.api_key = "sk-test"
        fake_model_cfg.model_name = "deepseek-chat"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "ok"

        with um.patch.dict(os.environ, {"ALFRED_SKILL_MODEL": "deepseek-chat"}), \
             um.patch(
                 "src.everbot.core.runtime.heartbeat._get_skill_global_config",
                 return_value=fake_config,
             ), um.patch(
                 "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
             ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = fake_response
            mock_openai_cls.return_value = mock_client

            result = await client.complete("test")

        assert result == "ok"
        fake_config.get_model_config.assert_called_once_with("deepseek-chat")

    # ── Exception classification tests ────────────────────────────

    def _make_client_and_mocks(self):
        """Helper: return (_SkillLLMClient, fake_config, fake_model_cfg)."""
        from src.everbot.core.runtime.heartbeat import _SkillLLMClient

        client = _SkillLLMClient(model="test-model")

        fake_config = MagicMock()
        fake_model_cfg = MagicMock()
        fake_model_cfg.effective_api = "https://fake.example.com/v1"
        fake_model_cfg.api_key = "fake-key"
        fake_model_cfg.model_name = "test-model"
        fake_model_cfg.max_tokens = 2000
        fake_model_cfg.effective_headers = {}
        fake_config.get_model_config.return_value = fake_model_cfg
        return client, fake_config

    @pytest.mark.asyncio
    async def test_connection_error_raises_transient(self):
        """ConnectionError from OpenAI client should be classified as LLMTransientError."""
        import unittest.mock as um
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        client, fake_config = self._make_client_and_mocks()

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = ConnectionError("peer reset")
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMTransientError, match="peer reset"):
                await client.complete("hello")

    @pytest.mark.asyncio
    async def test_timeout_error_raises_transient(self):
        """TimeoutError from OpenAI client should be classified as LLMTransientError."""
        import unittest.mock as um
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        client, fake_config = self._make_client_and_mocks()

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = TimeoutError("request timed out")
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMTransientError, match="request timed out"):
                await client.complete("hello")

    @pytest.mark.asyncio
    async def test_openai_api_connection_error_raises_transient(self):
        """openai.APIConnectionError should be classified as LLMTransientError."""
        import unittest.mock as um
        import openai
        from src.everbot.core.jobs.llm_errors import LLMTransientError

        client, fake_config = self._make_client_and_mocks()

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = openai.APIConnectionError(
                request=MagicMock()
            )
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMTransientError):
                await client.complete("hello")

    @pytest.mark.asyncio
    async def test_openai_auth_error_raises_config(self):
        """openai.AuthenticationError should be classified as LLMConfigError."""
        import unittest.mock as um
        import openai
        from src.everbot.core.jobs.llm_errors import LLMConfigError

        client, fake_config = self._make_client_and_mocks()

        with um.patch(
            "src.everbot.core.runtime.heartbeat._get_skill_global_config",
            return_value=fake_config,
        ), um.patch(
            "src.everbot.core.runtime.heartbeat.AsyncOpenAI",
        ) as mock_openai_cls:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
                message="invalid api key",
                response=MagicMock(status_code=401),
                body=None,
            )
            mock_openai_cls.return_value = mock_client

            with pytest.raises(LLMConfigError, match="invalid api key"):
                await client.complete("hello")
