"""
Session Restoration Integration Test
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.everbot.infra.user_data import UserDataManager
from src.everbot.core.session.session import SessionManager


@pytest.mark.asyncio
async def test_session_save_and_restore_workflow():
    """
    Integration test:
    1. Create an agent and context.
    2. Save session.
    3. Create a new agent and restore from session.
    4. Verify history and variables are preserved.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        user_data = UserDataManager(alfred_home=tmp_path)
        user_data.ensure_directories()
        
        # Setup session manager
        session_manager = SessionManager(user_data.sessions_dir)
        session_id = "test_integration_session"
        agent_name = "test_agent"
        
        # Initialize agent workspace
        user_data.init_agent_workspace(agent_name)
        
        # Mock DolphinAgent and Context
        # We use a real AgentFactory but we might need to mock the LLM if we were doing a full end-to-end.
        # Here we focus on the state restoration logic between everbot components and Dolphin SDK.
        
        mock_agent = MagicMock()
        mock_agent.name = agent_name
        mock_context = MagicMock()
        mock_agent.executor.context = mock_context
        
        # Set up some initial state
        mock_context.get_history_messages.return_value = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"}
        ]
        mock_context.get_var_value.side_effect = lambda x: {
            "workspace_instructions": "Be helpful.",
            "model_name": "gpt-4",
            "current_time": "2024-01-01",
            "session_created_at": "2024-01-01T00:00:00"
        }.get(x)
        
        # 1. Save session
        await session_manager.save_session(session_id, mock_agent, "gpt-4")
        assert (user_data.sessions_dir / f"{session_id}.json").exists()
        
        # 2. Load session data
        loaded_data = await session_manager.load_session(session_id)
        assert loaded_data is not None
        assert loaded_data.agent_name == agent_name
        assert len(loaded_data.history_messages) == 2
        
        # 3. Restore to a NEW agent
        new_mock_agent = MagicMock()
        new_mock_context = MagicMock()
        new_mock_agent.executor.context = new_mock_context
        
        await session_manager.restore_to_agent(new_mock_agent, loaded_data)
        
        # 4. Verify restoration calls
        restored_var_names = [call.args[0] for call in new_mock_context.set_variable.call_args_list]
        assert "workspace_instructions" not in restored_var_names
        new_mock_context.set_variable.assert_any_call("model_name", "gpt-4")
        new_mock_context.set_variable.assert_any_call("current_time", "2024-01-01")
        assert new_mock_context.set_history_bucket.called
        new_mock_context.set_variable.assert_any_call("session_id", session_id)
