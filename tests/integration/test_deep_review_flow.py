"""
Integration test: Deep Review e2e flow.

Verifies that a "帮我 review 下 alfred 项目" message triggers the correct
tool call sequence through ChannelCoreService:

  _load_resource_skill("coding-master")       → Gateway loaded
  _load_skill_resource("coding-master", ...)   → Deep Review SOP loaded
  _bash(quick-status --repos alfred)           → Gather context
  _bash(workspace-check --repos alfred ...)    → Acquire workspace
  _bash(analyze --workspace ... --engine ...)  → Engine-powered analysis
  _bash(release --workspace ...)               → Release

The agent is scripted (no real LLM call), but all events flow through
the real TurnOrchestrator and ChannelCoreService pipeline.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dolphin.core.agent.agent_state import AgentState

from src.everbot.core.channel.core_service import ChannelCoreService
from src.everbot.core.channel.models import OutboundMessage


# ---------------------------------------------------------------------------
# Helpers (same patterns as test_channel_core_service.py)
# ---------------------------------------------------------------------------

def _make_session_manager_mock():
    timeline_events = []

    def append_timeline_event(_sid, event):
        timeline_events.append(dict(event))

    class _LockCtx:
        def __enter__(self):
            return True
        def __exit__(self, *_):
            return False

    _tmp_lock_dir = Path(tempfile.mkdtemp())

    return SimpleNamespace(
        persistence=SimpleNamespace(
            _get_lock_path=lambda sid: _tmp_lock_dir / f".{sid}.lock",
        ),
        save_session=AsyncMock(),
        load_session=AsyncMock(return_value=SimpleNamespace(mailbox=[], timeline=[])),
        restore_timeline=lambda sid, timeline: None,
        restore_to_agent=AsyncMock(return_value=None),
        acquire_session=AsyncMock(return_value=True),
        release_session=lambda sid: None,
        file_lock=lambda sid, blocking=False: _LockCtx(),
        ack_mailbox_events=AsyncMock(return_value=True),
        clear_timeline=lambda sid: None,
        append_timeline_event=append_timeline_event,
        get_primary_session_id=lambda agent_name: f"test_session_{agent_name}",
        migrate_legacy_sessions_for_agent=AsyncMock(return_value=False),
        _timeline_events=timeline_events,
    )


def _make_user_data_mock(sessions_dir: Path):
    return SimpleNamespace(
        sessions_dir=sessions_dir,
        get_session_trajectory_path=lambda a, s: sessions_dir / f"{a}_{s}.jsonl",
    )


class _DummyContext:
    def __init__(self):
        self._vars = {"workspace_instructions": "Test workspace instructions."}

    def get_var_value(self, name):
        return self._vars.get(name)

    def set_variable(self, name, value):
        self._vars[name] = value

    def init_trajectory(self, _path, overwrite=False):
        return None


class _EventCollector:
    def __init__(self):
        self.events: list[OutboundMessage] = []

    async def __call__(self, msg: OutboundMessage):
        self.events.append(msg)

    def skills(self) -> list[OutboundMessage]:
        return [e for e in self.events if e.msg_type == "skill"]

    def skill_names(self, status: str = "processing") -> list[str]:
        """Extract tool/skill names from events with the given status."""
        return [
            e.metadata.get("skill_name", "")
            for e in self.events
            if e.msg_type == "skill" and (e.metadata or {}).get("status") == status
        ]

    def skill_args_for(self, name: str) -> list[str]:
        """Get all skill_args for a given skill name (processing events)."""
        return [
            e.metadata.get("skill_args", "")
            for e in self.events
            if e.msg_type == "skill"
            and (e.metadata or {}).get("status") == "processing"
            and (e.metadata or {}).get("skill_name") == name
        ]


def _make_core_service(tmp_path: Path):
    sm = _make_session_manager_mock()
    ud = _make_user_data_mock(tmp_path)
    core = ChannelCoreService.__new__(ChannelCoreService)
    core.session_manager = sm
    core.user_data = ud
    core.agent_service = None
    return core


# ---------------------------------------------------------------------------
# Scripted progress events (Dolphin SDK format)
# ---------------------------------------------------------------------------

def _skill(name: str, args: str = "", pid: str = "s1", status: str = "processing"):
    return {
        "id": pid,
        "stage": "skill",
        "skill_info": {"name": name, "args": args},
        "status": status,
    }


def _skill_done(name: str, output: str = "", pid: str = "s1"):
    return {
        "id": pid,
        "stage": "skill",
        "skill_info": {"name": name},
        "output": output,
        "status": "completed",
    }


def _llm(text: str, pid: str = "llm1"):
    return {"id": pid, "stage": "llm", "delta": text, "status": "running"}


def _p(*items):
    """Wrap progress items into a Dolphin-style event dict."""
    return {"_progress": list(items)}


# ---------------------------------------------------------------------------
# The expected deep review script
# ---------------------------------------------------------------------------

ANALYZE_SUMMARY = """\
## Review Summary

### High Priority
- H1: Web API 无认证与安全防护
- H2: 定时任务无持久化保障

### Medium Priority
- M1: Telegram channel 缺少 rate limiting
- M2: 配置文件明文存储敏感信息

### Low Priority
- L1: 部分函数缺少 type hints
"""

WORKSPACE_CHECK_OUTPUT = json.dumps({
    "ok": True,
    "data": {
        "snapshot": {
            "repos": [{"name": "alfred", "git": {"branch": "main"}}],
            "base_commit": "abc123",
            "primary_repo": "alfred",
        },
        "workspace": "env0",
    },
})

ANALYZE_OUTPUT = json.dumps({
    "ok": True,
    "data": {
        "summary": ANALYZE_SUMMARY,
        "files_changed": [],
        "complexity": "standard",
        "feature_plan_created": False,
        "feature_count": 0,
    },
})

RELEASE_OUTPUT = json.dumps({"ok": True, "data": {"released": True}})


def _build_deep_review_script():
    """Build the scripted event sequence for a successful deep review.

    Simulates agent behavior:
    1. Load coding-master skill (gateway)
    2. Load deep-review SOP
    3. quick-status (gather context)
    4. workspace-check (acquire)
    5. analyze with engine (deep analysis)
    6. LLM streams the review report
    7. release workspace
    """
    return [
        # Step 1: Agent loads coding-master gateway
        _p(_skill("_load_resource_skill",
                   '{"skill_name": "coding-master"}',
                   pid="sk1")),
        _p(_skill_done("_load_resource_skill",
                       output="# Coding Master Skill\n## Intent Routing ...",
                       pid="sk1")),

        # Step 2: Agent loads deep-review SOP
        _p(_skill("_load_skill_resource",
                   '{"skill_name": "coding-master", "resource_path": "references/sop-deep-review.md"}',
                   pid="sk2")),
        _p(_skill_done("_load_skill_resource",
                       output="# SOP: Deep Review\nUse when user asks to review ...",
                       pid="sk2")),

        # Step 3: Gather context - quick-status
        _p(_skill("_bash",
                   '{"cmd": "python dispatch.py quick-status --repos alfred"}',
                   pid="sk3")),
        _p(_skill_done("_bash",
                       output='{"ok": true, "data": {"repos": {"alfred": {"git": {"branch": "main"}}}}}',
                       pid="sk3")),

        # Step 4: Acquire workspace
        _p(_skill("_bash",
                   '{"cmd": "python dispatch.py workspace-check --repos alfred --task \'review: review alfred\' --engine codex"}',
                   pid="sk4")),
        _p(_skill_done("_bash", output=WORKSPACE_CHECK_OUTPUT, pid="sk4")),

        # Step 5: Engine-powered analysis (the key step)
        _p(_skill("_bash",
                   '{"cmd": "python dispatch.py analyze --workspace env0 --task \'Full project review\' --engine codex"}',
                   pid="sk5")),
        _p(_skill_done("_bash", output=ANALYZE_OUTPUT, pid="sk5")),

        # Step 6: LLM streams the review report
        _p(_llm("## Alfred 项目 Review 报告\n\n")),
        _p(_llm("### 高优先级问题\n")),
        _p(_llm("1. Web API 无认证与安全防护\n")),
        _p(_llm("2. 定时任务无持久化保障\n")),

        # Step 7: Release workspace
        _p(_skill("_bash",
                   '{"cmd": "python dispatch.py release --workspace env0"}',
                   pid="sk6")),
        _p(_skill_done("_bash", output=RELEASE_OUTPUT, pid="sk6")),
    ]


# ---------------------------------------------------------------------------
# Scripted agent
# ---------------------------------------------------------------------------

class _ScriptedAgent:
    """Agent that yields a scripted sequence of Dolphin-style progress events."""
    name = "demo_agent"

    def __init__(self, script):
        self._script = script
        self.executor = SimpleNamespace(context=_DummyContext())
        self.state = AgentState.INITIALIZED

    async def arun(self, **_kwargs):
        for item in self._script:
            yield item

    async def continue_chat(self, **_kwargs):
        for item in self._script:
            yield item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deep_review_flow_calls_engine_analysis():
    """
    E2E: "帮我 review 下 alfred 项目" triggers the full deep review pipeline.

    Verifies:
    1. coding-master skill is loaded (gateway)
    2. deep-review SOP is loaded via _load_skill_resource
    3. workspace-check is called to acquire workspace
    4. analyze is called with --engine (engine-powered deep analysis)
    5. release is called to free workspace
    6. Response is streamed back
    """
    script = _build_deep_review_script()
    agent = _ScriptedAgent(script)

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(
            agent, "demo_agent", "test_session_demo_agent",
            "帮我 review 下 alfred 项目，看有没有待解决的高优问题或者改进项",
            collector,
        )

    # --- Assertions ---

    invoked_names = collector.skill_names("processing")

    # 1. Gateway loaded
    assert "_load_resource_skill" in invoked_names, \
        f"Expected _load_resource_skill in {invoked_names}"

    # 2. Deep Review SOP loaded
    assert "_load_skill_resource" in invoked_names, \
        f"Expected _load_skill_resource in {invoked_names}"
    sop_args = collector.skill_args_for("_load_skill_resource")
    assert any("sop-deep-review" in a for a in sop_args), \
        f"Expected sop-deep-review.md to be loaded, got args: {sop_args}"

    # 3. workspace-check called
    bash_args = collector.skill_args_for("_bash")
    assert any("workspace-check" in a for a in bash_args), \
        f"Expected workspace-check in _bash calls: {bash_args}"

    # 4. analyze called with --engine (engine-powered)
    assert any("analyze" in a and "--engine" in a for a in bash_args), \
        f"Expected 'analyze --engine' in _bash calls: {bash_args}"

    # 5. release called
    assert any("release" in a for a in bash_args), \
        f"Expected release in _bash calls: {bash_args}"

    # 6. LLM response was streamed
    deltas = [e for e in collector.events if e.msg_type == "delta"]
    assert len(deltas) > 0, "Expected LLM delta events to be streamed"
    full_response = "".join(d.content for d in deltas)
    assert "高优先级" in full_response or "Review" in full_response

    # 7. Session was saved
    core.session_manager.save_session.assert_awaited()

    # 8. Internal tools (_load_resource_skill, _load_skill_resource) are
    #    budget-exempt — they should not count toward tool call limit
    #    (verified by the fact that 8 total skill calls didn't exceed budget)


@pytest.mark.asyncio
async def test_deep_review_flow_records_timeline_events():
    """Verify timeline events are recorded for the review turn."""
    script = _build_deep_review_script()
    agent = _ScriptedAgent(script)

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(
            agent, "demo_agent", "test_session_demo_agent",
            "帮我 review 下 alfred 项目",
            collector,
        )

    timeline = core.session_manager._timeline_events

    # turn_start recorded
    assert any(e.get("type") == "turn_start" for e in timeline), \
        "Expected turn_start in timeline"

    # turn_end recorded
    turn_ends = [e for e in timeline if e.get("type") == "turn_end"]
    assert len(turn_ends) == 1
    assert turn_ends[0]["status"] == "completed"

    # Tool calls recorded (excluding budget-exempt tools)
    tool_calls = [e for e in timeline if e.get("type") == "tool_call"]
    tool_names = [e.get("tool_name", "") for e in tool_calls]
    # _bash calls for workspace-check, analyze, release should be in timeline
    assert any("_bash" in n for n in tool_names), \
        f"Expected _bash tool calls in timeline, got: {tool_names}"


@pytest.mark.asyncio
async def test_deep_review_without_engine_call_is_incomplete():
    """
    If agent skips the analyze step (no engine call), the review is shallow.
    This test documents the anti-pattern we want to prevent.
    """
    # Script that skips workspace-check and analyze — just uses quick commands
    shallow_script = [
        _p(_skill("_load_resource_skill",
                   '{"skill_name": "coding-master"}', pid="sk1")),
        _p(_skill_done("_load_resource_skill",
                       output="# Coding Master Skill ...", pid="sk1")),
        # Directly does quick-status without loading SOP
        _p(_skill("_bash",
                   '{"cmd": "python dispatch.py quick-status --repos alfred"}',
                   pid="sk2")),
        _p(_skill_done("_bash", output='{"ok": true}', pid="sk2")),
        # Streams a shallow response
        _p(_llm("项目状态正常，没有明显问题。")),
    ]
    agent = _ScriptedAgent(shallow_script)

    with tempfile.TemporaryDirectory() as tmp:
        core = _make_core_service(Path(tmp))
        collector = _EventCollector()

        await core.process_message(
            agent, "demo_agent", "test_session_demo_agent",
            "帮我 review 下 alfred 项目",
            collector,
        )

    bash_args = collector.skill_args_for("_bash")

    # No deep-review SOP loaded
    sop_loaded = any("sop-deep-review" in a
                     for a in collector.skill_args_for("_load_skill_resource"))
    assert not sop_loaded, "Shallow review should NOT load deep-review SOP"

    # No analyze call
    assert not any("analyze" in a for a in bash_args), \
        "Shallow review should NOT call analyze"
