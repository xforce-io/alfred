
import pytest
import tempfile
import json
from pathlib import Path
from dolphin.core.context.context import Context
from dolphin.core.common.enums import Messages, MessageRole
from dolphin.core.common.constants import KEY_HISTORY
from src.everbot.core.session.session import SessionManager, SessionData

@pytest.mark.asyncio
async def test_repro_history_wipe_condition():
    """
    Reproduction test case for the history wipe issue.
    It simulates the state where the history variable pool has data (updated by Dolphin blocks),
    but the context messages (buckets) might be empty or not synced yet.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        
        # 1. Setup a real Dolphin Context
        context = Context()
        
        # 2. Simulate Dolphin's _update_history_and_cleanup behavior
        # It updates the KEY_HISTORY variable in the variable pool
        history_data = [
            {"role": "user", "content": "hello", "timestamp": "2024-01-01"},
            {"role": "assistant", "content": "hi there", "timestamp": "2024-01-01"}
        ]
        # Dolphin uses KEY_HISTORY (usually "_history")
        context.set_variable(KEY_HISTORY, history_data)
        
        # Test the fallback
        assert context.get_var_value("history") == history_data
        
        # At this point, context.get_messages() is EMPTY because buckets haven't been synced from the variable pool
        # and no one has called set_history_bucket yet.
        assert len(context.get_messages().get_messages()) == 0
        
        # 3. Create a mock agent
        class MockAgent:
            def __init__(self, ctx):
                self.executor = type('obj', (object,), {'context': ctx})
                self.name = "repro_agent"
        
        agent = MockAgent(context)
        session_id = "test_persistence"
        
        # 4. Save session
        # This SHOULD use the history from the variable pool (via get_history_messages), not the empty bucket mirror.
        await manager.save_session(session_id, agent)
        
        # 5. Verify the saved file
        session_file = session_dir / f"{session_id}.json"
        assert session_file.exists()
        
        with open(session_file, "r") as f:
            saved_data = json.load(f)
            # The saved history MUST NOT be empty
            assert len(saved_data["history_messages"]) == 2
            assert saved_data["history_messages"][0]["content"] == "hello"

@pytest.mark.asyncio
async def test_restore_and_immediate_save():
    """
    Test the 'Restore and Save' cycle that was causing the wipe.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        
        session_id = "restore_save_cycle"
        history = [{"role": "user", "content": "persistence test"}]
        
        # Create initial session data
        data = SessionData(
            session_id=session_id,
            agent_name="test_agent",
            model_name="test_model",
            history_messages=history,
            variables={},
            created_at="2024-01-01",
            updated_at="2024-01-01"
        )
        
        context = Context()
        class MockAgent:
            def __init__(self, ctx):
                self.executor = type('obj', (object,), {'context': ctx})
                self.name = "test_agent"
        agent = MockAgent(context)
        
        # Phase 1: Restore
        await manager.restore_to_agent(agent, data)
        
        # Phase 2: Immediate Save
        await manager.save_session(session_id, agent)
        
        # Phase 3: Verify disk content
        loaded = await manager.load_session(session_id)
        assert len(loaded.history_messages) == 1
        assert loaded.history_messages[0]["content"] == "persistence test"


@pytest.mark.asyncio
async def test_load_preserves_history_order_when_timestamps_are_non_monotonic():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        session_id = "non_monotonic_ts_order"
        session_file = session_dir / f"{session_id}.json"

        raw = {
            "session_id": session_id,
            "agent_name": "demo_agent",
            "model_name": "test_model",
            "history_messages": [
                {"role": "user", "content": "Q1", "timestamp": "2026-02-08T21:43:25.199537"},
                {"role": "assistant", "content": "A1", "timestamp": "2026-02-08T21:40:50.796306"},
                {"role": "tool", "content": "tool-out", "timestamp": "2026-02-08T21:40:50.800202"},
            ],
            "variables": {},
            "created_at": "2026-02-08T21:40:00.000000",
            "updated_at": "2026-02-08T21:44:00.000000",
            "timeline": [],
            "context_trace": {},
        }
        session_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        loaded = await manager.load_session(session_id)
        assert loaded is not None
        assert [m["content"] for m in loaded.history_messages] == ["Q1", "A1", "tool-out"]
