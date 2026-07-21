"""
Unit tests for ChannelCoreService.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.everbot.infra.dolphin_compat import KEY_HISTORY


class AgentState:  # #38:dolphin 已移除;mock agent 状态用本地桩(只需 .INITIALIZED 标记)
    INITIALIZED = "initialized"

from src.everbot.core.channel.core_service import ChannelCoreService
from src.everbot.core.channel.models import OutboundMessage


def _make_session_manager_mock():
    """Create a session_manager mock with all required attributes."""
    timeline_events = []

    def append_timeline_event(_sid, event):
        timeline_events.append(dict(event))

    class _LockCtx:
        def __enter__(self):
            return True

        def __exit__(self, _exc_type, _exc_val, _exc_tb):
            return False

    _tmp_lock_dir = Path(tempfile.mkdtemp())

    persistence_mock = SimpleNamespace(
        _get_lock_path=lambda session_id: _tmp_lock_dir / f".{session_id}.lock",
    )

    return SimpleNamespace(
        persistence=persistence_mock,
        save_session=AsyncMock(),
        load_session=AsyncMock(return_value=SimpleNamespace(mailbox=[], timeline=[])),
        restore_timeline=lambda sid, timeline: None,
        restore_to_agent=AsyncMock(return_value=None),
        acquire_session=AsyncMock(return_value=True),
        release_session=lambda sid: None,
        file_lock=lambda sid, blocking=False: _LockCtx(),
        ack_mailbox_events=AsyncMock(return_value=True),
        inject_history_message=AsyncMock(return_value=True),
        deposit_mailbox_event=AsyncMock(return_value=True),
        clear_timeline=lambda sid: None,
        append_timeline_event=append_timeline_event,
        get_primary_session_id=lambda agent_name: f"web_session_{agent_name}",
        migrate_legacy_sessions_for_agent=AsyncMock(return_value=False),
        _timeline_events=timeline_events,
    )


def _make_user_data_mock(sessions_dir: Path):
    """Create a user_data mock with sessions_dir."""
    def _get_session_trajectory_path(agent_name: str, session_id: str) -> Path:
        return sessions_dir / f"{agent_name}_{session_id}.jsonl"

    return SimpleNamespace(
        sessions_dir=sessions_dir,
        get_session_trajectory_path=_get_session_trajectory_path,
    )


class _DummyContext:
    def __init__(self):
        self._vars = {"workspace_instructions": "Test workspace instructions."}

    def get_var_value(self, name: str):
        return self._vars.get(name)

    def set_variable(self, _name: str, _value):
        self._vars[_name] = _value
        return None

    def init_trajectory(self, _path: str, overwrite: bool = False):  # noqa: ARG002
        return None


class _DummyAgent:
    name = "dummy_agent"

    def __init__(self, events):
        self._events = events
        self.executor = SimpleNamespace(context=_DummyContext())
        self.state = AgentState.INITIALIZED

    async def continue_chat(self, **_kwargs):
        for event in self._events:
            yield event


class _CallPathAgent:
    name = "dummy_agent"

    def __init__(self, history_messages):
        ctx = _DummyContext()
        ctx.set_variable(KEY_HISTORY, history_messages)
        self.executor = SimpleNamespace(context=ctx)
        self.state = AgentState.INITIALIZED
        self.arun_calls = []
        self.continue_calls = []

    async def arun(self, **kwargs):
        self.arun_calls.append(kwargs)
        yield {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "OK"}]}

    async def continue_chat(self, **kwargs):
        self.continue_calls.append(kwargs)
        yield {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "OK"}]}


class _FailingAgent:
    name = "dummy_agent"

    def __init__(self):
        self.executor = SimpleNamespace(context=_DummyContext())
        self.state = AgentState.INITIALIZED

    async def continue_chat(self, **_kwargs):
        raise RuntimeError("boom")
        yield {}  # pragma: no cover


class _TurnErrorAgent:
    name = "dummy_agent"

    def __init__(self):
        self.executor = SimpleNamespace(context=_DummyContext())
        self.state = AgentState.INITIALIZED

    async def continue_chat(self, **_kwargs):
        if False:
            yield {}


def _make_core_service(tmp_path: Path):
    """Create a ChannelCoreService with mocked dependencies."""
    sm = _make_session_manager_mock()
    ud = _make_user_data_mock(tmp_path)
    core = ChannelCoreService.__new__(ChannelCoreService)
    core.session_manager = sm
    core.user_data = ud
    core.agent_service = None
    core._session_failure_memory = {}
    return core


class _EventCollector:
    """Collects OutboundMessage events for assertions."""

    def __init__(self):
        self.events: list[OutboundMessage] = []

    async def __call__(self, msg: OutboundMessage):
        self.events.append(msg)

    def payloads_by_type(self, msg_type: str) -> list[OutboundMessage]:
        return [e for e in self.events if e.msg_type == msg_type]

    @property
    def last(self) -> OutboundMessage:
        return self.events[-1]


@pytest.mark.asyncio
async def test_process_message_calls_on_event_with_delta():
    """LLM_DELTA events are delivered as delta OutboundMessages."""
    agent = _DummyAgent(
        events=[
            {"workspace_instructions": "spam"},
            {"model_name": "qwen-plus"},
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    deltas = collector.payloads_by_type("delta")
    assert any(d.content == "Hi" for d in deltas)
    assert any(
        event.get("source_type") == "chat_user" and event.get("run_id")
        for event in core.session_manager._timeline_events
    )


@pytest.mark.asyncio
async def test_process_message_calls_on_event_with_end():
    """Turn completion sends an end OutboundMessage."""
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    ends = collector.payloads_by_type("end")
    assert len(ends) >= 1


@pytest.mark.asyncio
async def test_process_message_busy_when_lock_fails():
    """When acquire_session returns False, a busy status + end is sent."""
    agent = _DummyAgent(events=[])

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        core.session_manager.acquire_session = AsyncMock(return_value=False)
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    statuses = collector.payloads_by_type("status")
    assert any("繁忙" in s.content for s in statuses)
    assert collector.last.msg_type == "end"


@pytest.mark.asyncio
async def test_process_message_error_sends_error_outbound():
    """When run_turn raises, error + end OutboundMessages are sent."""
    agent = _FailingAgent()

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    texts = collector.payloads_by_type("text")
    assert any("本轮执行遇到错误" in t.content for t in texts)
    assert collector.last.msg_type == "end"


@pytest.mark.asyncio
async def test_process_message_repeated_tool_failures_sends_generic_guidance(monkeypatch):
    """Repeated tool failures should produce a generic strategy-switch hint."""
    agent = _TurnErrorAgent()

    class _FakeTurnOrchestrator:
        def __init__(self, _policy, **_kw):
            self.accumulated_failures = {}

        async def run_turn(self, *_args, **_kwargs):
            from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType
            yield TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error="REPEATED_TOOL_FAILURES: failed=4, signature=exit_code:2, count=4",
                tool_call_count=8,
                tool_names_executed=["web_search", "bash"],
                failed_tool_outputs=4,
            )

    monkeypatch.setattr("src.everbot.core.channel.core_service.TurnOrchestrator", _FakeTurnOrchestrator)

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    texts = collector.payloads_by_type("text")
    # Should contain structured summary with tool call stats
    assert any("已停止本轮自动重试" in t.content for t in texts)
    assert any("8 次工具调用" in t.content for t in texts)
    assert any("4 次失败" in t.content for t in texts)


@pytest.mark.asyncio
async def test_process_message_saves_session_after_turn():
    """save_session is called after a successful turn."""
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    core.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_deferred_result_emit_uses_explicit_target_fields(monkeypatch):
    """Deferred result events should carry explicit routing metadata."""
    agent = _TurnErrorAgent()
    emitted = []

    class _FakeTurnOrchestrator:
        def __init__(self, _policy, **_kw):
            self.accumulated_failures = {}

        async def run_turn(self, *_args, **kwargs):
            on_deferred_result = kwargs.get("on_deferred_result")
            assert on_deferred_result is not None
            await on_deferred_result("Deferred answer")
            if False:
                yield None

    async def _fake_emit(source_session_id, data, **kwargs):
        emitted.append((source_session_id, data, kwargs))

    monkeypatch.setattr("src.everbot.core.channel.core_service.TurnOrchestrator", _FakeTurnOrchestrator)
    monkeypatch.setattr("src.everbot.core.channel.core_service.events.emit", _fake_emit)

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    assert len(emitted) == 1
    source_session_id, data, kwargs = emitted[0]
    assert source_session_id == "web_session_demo_agent"
    assert data["source_type"] == "deferred_result"
    assert kwargs["scope"] == "session"
    assert kwargs["target_session_id"] == "web_session_demo_agent"
    assert kwargs["target_channel"] == "web"


@pytest.mark.asyncio
async def test_process_message_acks_mailbox():
    """Mailbox events with ack_ids are acknowledged after turn."""
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "Hi"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        # Setup session with mailbox events
        core.session_manager.load_session = AsyncMock(return_value=SimpleNamespace(
            mailbox=[
                {"event_id": "evt1", "detail": "background task done", "source_agent": "bg"},
            ],
            timeline=[],
        ))
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    core.session_manager.ack_mailbox_events.assert_awaited()


@pytest.mark.asyncio
async def test_process_message_acks_mailbox_on_error():
    """Mailbox events must be acked even when the turn fails with an error."""
    agent = _FailingAgent()

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        # Setup session with mailbox events that should be acked despite error
        core.session_manager.load_session = AsyncMock(return_value=SimpleNamespace(
            mailbox=[
                {"event_id": "evt_err", "event_type": "job_completed",
                 "summary": "daily news", "detail": "news content"},
            ],
            timeline=[],
        ))
        # Mock export_portable_session for the error save path
        agent.snapshot = SimpleNamespace(
            export_portable_session=lambda: {
                "history_messages": [],
                "variables": {},
            },
        )
        collector = _EventCollector()

        await core.process_message(agent, "demo_agent", "web_session_demo_agent", "hi", collector)

    # The turn errored, but mailbox should still be acked
    core.session_manager.ack_mailbox_events.assert_awaited()


# ---------------------------------------------------------------------------
# Bug: multimodal messages skip mailbox consumption, causing heartbeat events
# to accumulate and leak into the next text turn.
#
# Incident: user sent a paper screenshot (multimodal) → bot discussed the
# paper → user replied "好的，我也好奇具体怎么做的" (text).  A heartbeat result
# about "反共识信号已生成" had been deposited into the mailbox before the
# multimodal turn, but the multimodal turn skipped mailbox composition
# (isinstance(message, list) → mailbox_ack_ids = []).  The heartbeat event
# survived into the next text turn, was prepended to the user's message,
# and hijacked the LLM's intent resolution.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multimodal_message_skips_mailbox_ack_bug():
    """Multimodal messages skip mailbox composition and do NOT ack events.

    This means pending mailbox events survive across the multimodal turn
    and leak into the next text message — potentially hijacking user intent.
    """
    agent = _DummyAgent(
        events=[
            {"_progress": [{"id": "p1", "status": "running", "stage": "llm", "delta": "OK"}]},
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        core.session_manager.load_session = AsyncMock(return_value=SimpleNamespace(
            mailbox=[
                {"event_id": "evt_hb", "event_type": "heartbeat_result",
                 "summary": "每日反共识信号已顺利生成",
                 "detail": "反共识信号已顺利生成，包含三个核心信号"},
            ],
            timeline=[],
        ))
        collector = _EventCollector()

        # Send a multimodal message (image)
        multimodal_msg = [
            {"type": "text", "text": "[图片]"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/..."}},
        ]
        await core.process_message(
            agent, "demo_agent", "tg_session_demo_agent__123",
            multimodal_msg, collector,
        )

    # FIXED: mailbox events must be acked even for multimodal messages
    # so they don't accumulate and leak into the next text turn.
    ack_call_args = core.session_manager.ack_mailbox_events.call_args
    assert ack_call_args is not None, "ack_mailbox_events should be called"
    ack_ids = ack_call_args[0][1] if len(ack_call_args[0]) > 1 else []
    assert "evt_hb" in ack_ids, (
        f"Heartbeat event should be acked even for multimodal messages, got {ack_ids}"
    )


class TestWorkspaceInstructionsHelpersMilkieSafe:
    """_reload_workspace_instructions_if_missing / _cache_runtime_workspace_instructions
    must NOT crash on a milkie handle (no .executor).

    dolphin: in-process context (unchanged).
    milkie: workspace_instructions baked into agent.md; reload is a no-op,
    cache routes through provider.get_variable (may be None).
    """

    def _patch_provider(self, monkeypatch, provider):
        # core_service.py now dispatches operations via provider_for(agent)
        # (per-agent type routing) → patch that seam, not the global get_provider.
        import importlib
        cs_mod = importlib.import_module(ChannelCoreService.__module__)
        monkeypatch.setattr(cs_mod, "provider_for", lambda agent: provider)

    def test_reload_skips_for_milkie(self, monkeypatch):
        class _MilkieProvider:
            def needs_history_restore(self):
                return False

        self._patch_provider(monkeypatch, _MilkieProvider())
        with tempfile.TemporaryDirectory() as tmpdir:
            core = _make_core_service(Path(tmpdir))
            handle = SimpleNamespace(base_url="http://x", context_id="c1")  # no .executor
            # Must not raise AttributeError; reload is dolphin-only.
            core._reload_workspace_instructions_if_missing(handle, "test_agent")

    def test_cache_routes_through_provider_for_milkie(self, monkeypatch):
        class _MilkieProvider:
            def needs_history_restore(self):
                return False

            def get_variable(self, agent, key):
                assert key == "workspace_instructions"
                return "MILKIE WS"

        self._patch_provider(monkeypatch, _MilkieProvider())
        with tempfile.TemporaryDirectory() as tmpdir:
            core = _make_core_service(Path(tmpdir))
            handle = SimpleNamespace(base_url="http://x", context_id="c1")  # no .executor
            core._cache_runtime_workspace_instructions(handle, "test_agent")
            assert core._runtime_workspace_instructions_by_agent.get("test_agent") == "MILKIE WS"

    def test_cache_tolerates_none_for_milkie(self, monkeypatch):
        class _MilkieProvider:
            def needs_history_restore(self):
                return False

            def get_variable(self, agent, key):
                return None

        self._patch_provider(monkeypatch, _MilkieProvider())
        with tempfile.TemporaryDirectory() as tmpdir:
            core = _make_core_service(Path(tmpdir))
            handle = SimpleNamespace(base_url="http://x", context_id="c1")
            core._cache_runtime_workspace_instructions(handle, "test_agent")
            # None must not be cached.
            assert core._runtime_workspace_instructions_by_agent.get("test_agent", "") == ""

    def test_cache_reads_context_for_dolphin(self, monkeypatch):
        class _DolphinProvider:
            def needs_history_restore(self):
                return True

        self._patch_provider(monkeypatch, _DolphinProvider())
        with tempfile.TemporaryDirectory() as tmpdir:
            core = _make_core_service(Path(tmpdir))
            ctx = SimpleNamespace(
                get_var_value=lambda k: "DOLPHIN WS" if k == "workspace_instructions" else None
            )
            agent = SimpleNamespace(executor=SimpleNamespace(context=ctx))
            core._cache_runtime_workspace_instructions(agent, "test_agent")
            assert core._runtime_workspace_instructions_by_agent.get("test_agent") == "DOLPHIN WS"


# ---------------------------------------------------------------------------
# Issue #168: soft-timeout copy, turn_end stats, deferred delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_soft_timeout_user_message_semantics(monkeypatch):
    """S1: Soft-timeout copy must say background continues + auto-push; not hard-fail."""
    agent = _TurnErrorAgent()

    class _FakeTurnOrchestrator:
        def __init__(self, _policy, **_kw):
            self.accumulated_failures = {}

        async def run_turn(self, *_args, **_kwargs):
            from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType
            yield TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error="Turn exceeded 0.3s timeout",
                status="timeout",
                tool_call_count=5,
                tool_names_executed=["_bash", "search"],
                failed_tool_outputs=0,
            )

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.TurnOrchestrator",
        _FakeTurnOrchestrator,
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()
        await core.process_message(
            agent, "demo_agent", "web_session_demo_agent", "hi", collector,
        )

    texts = [t.content for t in collector.payloads_by_type("text")]
    assert texts, "expected a soft-timeout text outbound"
    joined = "\n".join(texts)
    assert "后台" in joined and "继续" in joined
    assert any(k in joined for k in ("自动推送", "推送到本会话", "推送结果"))
    assert "未能完成处理" not in joined
    assert "执行失败" not in joined

    # Hard-fail stack bubble must not be the primary conclusion for soft timeout
    errors = collector.payloads_by_type("error")
    assert not errors, f"soft timeout must not emit error payload: {errors}"


@pytest.mark.asyncio
async def test_soft_timeout_turn_end_status_and_tool_count(monkeypatch):
    """S3: turn_end on soft timeout has status=timeout and tool_call_count from orchestrator."""
    agent = _TurnErrorAgent()

    class _FakeTurnOrchestrator:
        def __init__(self, _policy, **_kw):
            self.accumulated_failures = {}

        async def run_turn(self, *_args, **_kwargs):
            from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType
            # Skill-stage tools never increment core_service local counters;
            # stats only arrive on TURN_ERROR from the orchestrator.
            yield TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error="Turn exceeded 600s timeout",
                status="timeout",
                tool_call_count=8,
                tool_execution_count=8,
                tool_names_executed=["a", "b", "c", "d", "e", "f", "g", "h"],
                failed_tool_outputs=0,
            )

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.TurnOrchestrator",
        _FakeTurnOrchestrator,
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()
        await core.process_message(
            agent, "demo_agent", "web_session_demo_agent", "long task", collector,
        )

    timeline = core.session_manager._timeline_events
    turn_ends = [e for e in timeline if e.get("type") == "turn_end" or e.get("event_type") == "turn_end"]
    # append_timeline_event stores the event dict as passed
    if not turn_ends:
        # Event may store type under different keys depending on _record_timeline_event
        turn_ends = [e for e in timeline if "turn_end" in str(e.get("type", e.get("event", "")))]
    assert turn_ends, f"expected turn_end in timeline, got: {timeline}"
    te = turn_ends[-1]
    assert te.get("tool_call_count") == 8, te
    assert te.get("status") == "timeout", te


@pytest.mark.asyncio
async def test_soft_timeout_end_to_end_drain_history_and_timeline(monkeypatch):
    """S2 integration path: short timeout → soft copy → deferred final → history + emit.

    Uses a real TurnOrchestrator with a scripted agent (mock channel sink).
    """
    import asyncio
    from src.everbot.core.runtime.turn_policy import TurnPolicy

    final_text = "DEFERRED_FINAL_ANSWER_168"
    emitted = []

    class _SlowFinishAgent:
        name = "dummy_agent"

        def __init__(self):
            self.executor = SimpleNamespace(context=_DummyContext())
            self.state = AgentState.INITIALIZED

        async def continue_chat(self, **_kwargs):
            # Two skill tools (count as N=2 in orchestrator, not local core_service)
            for i in range(2):
                yield {
                    "_progress": [{
                        "id": f"sk{i}",
                        "stage": "skill",
                        "status": "processing",
                        "skill_info": {"name": f"tool_{i}", "args": f"a{i}"},
                    }],
                }
                yield {
                    "_progress": [{
                        "id": f"sk{i}",
                        "stage": "skill",
                        "status": "completed",
                        "skill_info": {"name": f"tool_{i}"},
                        "output": f"out{i}",
                    }],
                }
            await asyncio.sleep(0.35)
            yield {
                "_progress": [{
                    "id": "llm1",
                    "stage": "llm",
                    "status": "running",
                    "delta": final_text,
                }],
            }

    def _short_policy(*_a, **_k):
        return TurnPolicy(
            max_attempts=1,
            timeout_seconds=0.15,
            drain_extra_seconds=2.0,
            max_tool_calls=50,
            max_same_tool_intent=50,
        )

    async def _fake_emit(source_session_id, data, **kwargs):
        emitted.append((source_session_id, data, kwargs))

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.build_chat_policy",
        _short_policy,
    )
    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.events.emit",
        _fake_emit,
    )

    agent = _SlowFinishAgent()
    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()
        await core.process_message(
            agent, "demo_agent", "web_session_demo_agent", "run long", collector,
        )

        # Soft-timeout copy first
        texts = [t.content for t in collector.payloads_by_type("text")]
        assert texts, "expected soft-timeout text"
        assert any("后台" in t and "继续" in t for t in texts)

        # turn_end stats
        timeline = core.session_manager._timeline_events
        turn_ends = [
            e for e in timeline
            if e.get("type") == "turn_end" or e.get("event_type") == "turn_end"
        ]
        assert turn_ends, timeline
        te = turn_ends[-1]
        assert te.get("status") == "timeout", te
        assert te.get("tool_call_count") == 2, te

        # Wait for deferred drain
        for _ in range(40):
            if emitted:
                break
            await asyncio.sleep(0.1)

        assert emitted, "deferred_result must be emitted after drain"
        _sid, data, kwargs = emitted[0]
        assert data.get("source_type") == "deferred_result"
        assert final_text in data.get("detail", "")
        assert kwargs.get("source_type") == "deferred_result"
        assert kwargs.get("scope") == "session"
        assert kwargs.get("target_session_id") == "web_session_demo_agent"

        core.session_manager.inject_history_message.assert_awaited()
        inj_args = core.session_manager.inject_history_message.await_args
        msg = inj_args.args[1] if inj_args.args and len(inj_args.args) > 1 else inj_args.kwargs.get("message")
        assert msg is not None
        assert msg.get("metadata", {}).get("source") == "deferred_result"
        assert final_text in msg.get("content", "")
        assert "超时后台任务完成后自动生成" in msg.get("content", "")


@pytest.mark.asyncio
async def test_soft_timeout_deferred_persists_to_real_session_history(monkeypatch):
    """S2 E2E: real SessionManager history is readable after deferred delivery.

    Uses a real SessionManager (disk-backed) + mock channel sink. After soft
    timeout and drain, the deferred final must be loadable from history and
    available as context for a subsequent turn.
    """
    import asyncio
    from unittest.mock import MagicMock

    from src.everbot.core.runtime.turn_policy import TurnPolicy
    from src.everbot.core.session.session import SessionManager
    from src.everbot.infra.dolphin_compat import KEY_HISTORY

    final_text = "PERSISTED_DEFERRED_FINAL_168"
    session_id = "web_session_demo_agent"
    emitted = []

    class _StubProvider:
        """In-memory provider so process_message does not need milkie serve."""

        def __init__(self):
            self._vars: dict = {KEY_HISTORY: []}

        def is_paused(self, _agent):
            return False

        def is_error(self, _agent):
            return False

        def set_variable(self, _agent, key, value):
            self._vars[key] = value

        def get_variable(self, _agent, key):
            return self._vars.get(key)

        def set_session_id(self, agent, session_id_):
            agent.context_id = session_id_

        def init_trajectory(self, *_a, **_k):
            return None

        def needs_history_restore(self):
            return True

        def export_session(self, _agent):
            return {
                "history_messages": list(self._vars.get(KEY_HISTORY) or []),
                "variables": dict(self._vars),
            }

        def restore_history(self, _agent, history):
            self._vars[KEY_HISTORY] = list(history or [])

    stub = _StubProvider()

    class _SlowFinishAgent:
        name = "demo_agent"

        def __init__(self):
            self.executor = SimpleNamespace(context=_DummyContext())
            self.state = AgentState.INITIALIZED
            self.context_id = "unset"
            self.base_url = "http://stub"

        async def continue_chat(self, **_kwargs):
            # One internal resource tool + two visible tools → user-visible N=2
            yield {
                "_progress": [{
                    "id": "sk_int",
                    "stage": "skill",
                    "status": "processing",
                    "skill_info": {"name": "_load_resource_skill", "args": "x"},
                }],
            }
            yield {
                "_progress": [{
                    "id": "sk_int",
                    "stage": "skill",
                    "status": "completed",
                    "skill_info": {"name": "_load_resource_skill"},
                    "output": "skill md",
                }],
            }
            for i in range(2):
                yield {
                    "_progress": [{
                        "id": f"sk{i}",
                        "stage": "skill",
                        "status": "processing",
                        "skill_info": {"name": f"tool_{i}", "args": f"a{i}"},
                    }],
                }
                yield {
                    "_progress": [{
                        "id": f"sk{i}",
                        "stage": "skill",
                        "status": "completed",
                        "skill_info": {"name": f"tool_{i}"},
                        "output": f"out{i}",
                    }],
                }
            await asyncio.sleep(0.35)
            yield {
                "_progress": [{
                    "id": "llm1",
                    "stage": "llm",
                    "status": "running",
                    "delta": final_text,
                }],
            }

    def _short_policy(*_a, **_k):
        return TurnPolicy(
            max_attempts=1,
            timeout_seconds=0.15,
            drain_extra_seconds=2.0,
            max_tool_calls=50,
            max_same_tool_intent=50,
        )

    async def _fake_emit(source_session_id, data, **kwargs):
        emitted.append((source_session_id, data, kwargs))

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.build_chat_policy",
        _short_policy,
    )
    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.events.emit",
        _fake_emit,
    )
    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.provider_for",
        lambda _agent: stub,
    )
    # save_session may still talk to provider; keep it lightweight via real SM
    # but stub export used on error path.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sm = SessionManager(tmp_path)
        ud = _make_user_data_mock(tmp_path)
        core = ChannelCoreService.__new__(ChannelCoreService)
        core.session_manager = sm
        core.user_data = ud
        core.agent_service = None
        core._session_failure_memory = {}
        # Avoid full WorkspaceLoader / strategy init paths that need config
        core._primary_context_strategy = MagicMock()
        core._primary_context_strategy.build_system_prompt = MagicMock(return_value=None)
        core._runtime_deps = MagicMock()

        collector = _EventCollector()
        agent = _SlowFinishAgent()
        await core.process_message(
            agent, "demo_agent", session_id, "run long real history", collector,
        )

        texts = [t.content for t in collector.payloads_by_type("text")]
        assert texts and any("后台" in t and "继续" in t for t in texts)

        # Wait for deferred drain + real inject_history_message
        for _ in range(40):
            if emitted:
                break
            await asyncio.sleep(0.1)
        assert emitted, "deferred_result must be emitted"

        # --- Read back real persisted history ---
        history = await core.load_history(session_id)
        deferred_msgs = [
            m for m in history
            if isinstance(m, dict)
            and (m.get("metadata") or {}).get("source") == "deferred_result"
        ]
        assert deferred_msgs, f"expected deferred_result in history, got: {history}"
        assert final_text in deferred_msgs[-1].get("content", "")
        assert "超时后台任务完成后自动生成" in deferred_msgs[-1].get("content", "")

        # turn_end tool_call_count is user-visible N=2 (internal loader excluded)
        # Timeline lives on SessionManager; collect from disk session if present
        session_data = await sm.load_session(session_id)
        assert session_data is not None
        # Timeline may be in-memory and/or persisted depending on save path
        timeline = []
        if hasattr(sm, "_timeline_events"):
            tl = sm._timeline_events
            if isinstance(tl, dict):
                timeline = list(tl.get(session_id) or [])
            elif isinstance(tl, list):
                timeline = list(tl)
        if not timeline and session_data.timeline:
            timeline = list(session_data.timeline)
        turn_ends = [
            e for e in timeline
            if isinstance(e, dict) and (
                e.get("type") == "turn_end" or e.get("event_type") == "turn_end"
            )
        ]
        assert turn_ends, f"expected turn_end, timeline={timeline}"
        te = turn_ends[-1]
        assert te.get("status") == "timeout", te
        assert te.get("tool_call_count") == 2, te

        # Subsequent turn context: history still carries deferred final
        history2 = await core.load_history(session_id)
        assert any(final_text in (m.get("content") or "") for m in history2 if isinstance(m, dict))
        # Provider-facing restore path would see the same messages
        stub.restore_history(agent, history2)
        restored = stub.get_variable(agent, KEY_HISTORY)
        assert any(
            final_text in (m.get("content") or "")
            for m in (restored or [])
            if isinstance(m, dict)
        )


@pytest.mark.asyncio
async def test_non_soft_timeout_error_not_promised_as_deferred(monkeypatch):
    """F-006: plain TimeoutError / 'timeout' text must not send soft-timeout copy.

    Agent/tool/provider timeouts do not start deferred-drain; users must not
    be told background work continues or that a result will auto-push.
    """
    agent = _TurnErrorAgent()

    class _FakeTurnOrchestrator:
        def __init__(self, _policy, **_kw):
            self.accumulated_failures = {}

        async def run_turn(self, *_args, **_kwargs):
            from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType
            # status intentionally not "timeout" — not a wrapper soft-timeout
            yield TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error="TimeoutError: provider request timed out after 30s",
                status="error",
                tool_call_count=2,
                tool_names_executed=["search", "fetch"],
                failed_tool_outputs=0,
            )

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.TurnOrchestrator",
        _FakeTurnOrchestrator,
    )

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()
        await core.process_message(
            agent, "demo_agent", "web_session_demo_agent", "search stuff", collector,
        )

    texts = [t.content for t in collector.payloads_by_type("text")]
    joined = "\n".join(texts)
    assert "后台" not in joined or "继续" not in joined, (
        f"must not promise background continuation for non-soft timeout: {joined!r}"
    )
    assert "自动推送" not in joined
    assert "推送到本会话" not in joined
    # Hard-failure path should be used
    assert any("未能完成处理" in t for t in texts) or collector.payloads_by_type("error")

    timeline = core.session_manager._timeline_events
    turn_ends = [
        e for e in timeline
        if e.get("type") == "turn_end" or e.get("event_type") == "turn_end"
    ]
    assert turn_ends, timeline
    assert turn_ends[-1].get("status") == "error", turn_ends[-1]
    assert turn_ends[-1].get("status") != "timeout"


@pytest.mark.asyncio
async def test_deferred_history_retries_while_session_lock_held(monkeypatch):
    """F-005 / S2: follow-up turn holding lock longer than inject timeout still lands history.

    inject_history_message may return False while another turn holds the session
    lock past a single 5s attempt. Delivery must retry and only emit after
    history is written.
    """
    import asyncio
    from unittest.mock import MagicMock

    import src.everbot.core.channel.core_service as core_mod
    from src.everbot.core.runtime.turn_policy import TurnPolicy
    from src.everbot.core.session.session import SessionManager

    final_text = "DEFERRED_AFTER_LOCK_CONTENTION_168"
    session_id = "web_session_demo_agent"
    emitted: list = []
    inject_attempts = {"n": 0}

    # Shorten only retry backoffs (s >= 1.0); leave agent hang sleep intact.
    _real_sleep = core_mod.asyncio.sleep

    async def _selective_sleep(s):
        if s >= 1.0:
            return None
        return await _real_sleep(s)

    monkeypatch.setattr(core_mod.asyncio, "sleep", _selective_sleep)

    class _StubProvider:
        def __init__(self):
            self._vars: dict = {KEY_HISTORY: []}

        def is_paused(self, _agent):
            return False

        def is_error(self, _agent):
            return False

        def set_variable(self, _agent, key, value):
            self._vars[key] = value

        def get_variable(self, _agent, key):
            return self._vars.get(key)

        def set_session_id(self, agent, session_id_):
            agent.context_id = session_id_

        def init_trajectory(self, *_a, **_k):
            return None

        def needs_history_restore(self):
            return True

        def export_session(self, _agent):
            return {
                "history_messages": list(self._vars.get(KEY_HISTORY) or []),
                "variables": dict(self._vars),
            }

        def restore_history(self, _agent, history):
            self._vars[KEY_HISTORY] = list(history or [])

    stub = _StubProvider()

    class _SlowFinishAgent:
        name = "demo_agent"

        def __init__(self):
            self.executor = SimpleNamespace(context=_DummyContext())
            self.state = AgentState.INITIALIZED
            self.context_id = "unset"
            self.base_url = "http://stub"

        async def continue_chat(self, **_kwargs):
            yield {
                "_progress": [{
                    "id": "sk0",
                    "stage": "skill",
                    "status": "processing",
                    "skill_info": {"name": "tool_0", "args": "a0"},
                }],
            }
            yield {
                "_progress": [{
                    "id": "sk0",
                    "stage": "skill",
                    "status": "completed",
                    "skill_info": {"name": "tool_0"},
                    "output": "out0",
                }],
            }
            await asyncio.sleep(0.35)
            yield {
                "_progress": [{
                    "id": "llm1",
                    "stage": "llm",
                    "status": "running",
                    "delta": final_text,
                }],
            }

    def _short_policy(*_a, **_k):
        return TurnPolicy(
            max_attempts=1,
            timeout_seconds=0.15,
            drain_extra_seconds=3.0,
            max_tool_calls=50,
            max_same_tool_intent=50,
        )

    async def _fake_emit(source_session_id, data, **kwargs):
        emitted.append((source_session_id, data, kwargs))

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.build_chat_policy",
        _short_policy,
    )
    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.events.emit",
        _fake_emit,
    )
    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.provider_for",
        lambda _agent: stub,
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sm = SessionManager(tmp_path)

        real_inject = sm.inject_history_message

        async def _contended_inject(sid, message, *, timeout=5.0, blocking=True):
            inject_attempts["n"] += 1
            # First two attempts fail as if lock held past inject timeout
            # (follow-up turn longer than one inject_timeout).
            if inject_attempts["n"] <= 2:
                return False
            return await real_inject(sid, message, timeout=timeout, blocking=blocking)

        sm.inject_history_message = _contended_inject  # type: ignore[method-assign]

        ud = _make_user_data_mock(tmp_path)
        core = ChannelCoreService.__new__(ChannelCoreService)
        core.session_manager = sm
        core.user_data = ud
        core.agent_service = None
        core._session_failure_memory = {}
        core._primary_context_strategy = MagicMock()
        core._primary_context_strategy.build_system_prompt = MagicMock(return_value=None)
        core._runtime_deps = MagicMock()

        collector = _EventCollector()
        agent = _SlowFinishAgent()
        await core.process_message(
            agent, "demo_agent", session_id, "run under contention", collector,
        )

        # Wait for deferred drain + retries
        for _ in range(60):
            if emitted:
                break
            await asyncio.sleep(0.1)

        assert inject_attempts["n"] >= 3, (
            f"expected retries under lock contention, got {inject_attempts['n']}"
        )
        assert emitted, "emit only after successful history inject; expected emit after retries"

        history = await core.load_history(session_id)
        deferred_msgs = [
            m for m in history
            if isinstance(m, dict)
            and (m.get("metadata") or {}).get("source") == "deferred_result"
        ]
        assert deferred_msgs, f"expected deferred_result in history after contention, got: {history}"
        assert final_text in deferred_msgs[-1].get("content", "")


@pytest.mark.asyncio
async def test_deferred_skips_emit_when_history_inject_never_succeeds(monkeypatch):
    """F-005: if history inject exhausts retries, do not emit deferred_result."""
    import src.everbot.core.channel.core_service as core_mod

    agent = _TurnErrorAgent()
    emitted: list = []
    inject_calls = {"n": 0}
    captured: dict = {}

    class _FakeTurnOrchestrator:
        def __init__(self, _policy, **_kw):
            self.accumulated_failures = {}

        async def run_turn(self, *_args, **kwargs):
            from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType
            captured["on_deferred"] = kwargs.get("on_deferred_result")
            yield TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error="Turn exceeded 0.3s timeout",
                status="timeout",
                tool_call_count=1,
            )

    async def _fake_emit(*_a, **_k):
        emitted.append((_a, _k))

    async def _always_fail_inject(*_a, **_k):
        inject_calls["n"] += 1
        return False

    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.TurnOrchestrator",
        _FakeTurnOrchestrator,
    )
    monkeypatch.setattr(
        "src.everbot.core.channel.core_service.events.emit",
        _fake_emit,
    )
    monkeypatch.setattr(core_mod, "DEFERRED_HISTORY_INJECT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(core_mod.asyncio, "sleep", _fast_sleep)

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        core.session_manager.inject_history_message = _always_fail_inject
        collector = _EventCollector()
        await core.process_message(
            agent, "demo_agent", "web_session_demo_agent", "hi", collector,
        )
        assert captured.get("on_deferred") is not None
        # Invoke deliver path directly (same closure used by drain callback).
        await captured["on_deferred"]("SHOULD_NOT_EMIT_WITHOUT_HISTORY")

    assert inject_calls["n"] >= 3, f"expected exhausted retries, got {inject_calls['n']}"
    assert not emitted, f"must not emit without durable history, got {emitted}"
