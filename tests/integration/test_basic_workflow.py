"""
Basic Workflow Integration Test
"""

import pytest
import tempfile
from pathlib import Path

from src.everbot.infra.user_data import UserDataManager
from src.everbot.infra.workspace import WorkspaceLoader


def test_integration_basic_workflow():
    """Integration test: Basic workspace initialization and loading workflow."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Initialize
        manager = UserDataManager(alfred_home=Path(tmpdir))
        manager.ensure_directories()

        # 2. Create Agent
        manager.init_agent_workspace("daily_insight")

        # 3. Load workspace
        workspace_path = manager.get_agent_dir("daily_insight")
        loader = WorkspaceLoader(workspace_path)
        instructions = loader.load()

        assert instructions.agents_md is not None
        assert "daily_insight" in instructions.agents_md

        # 4. Modify HEARTBEAT.md
        heartbeat_path = workspace_path / "HEARTBEAT.md"
        heartbeat_path.write_text("# Tasks\n- [ ] Task 1")

        instructions = loader.load()
        assert "Task 1" in instructions.heartbeat_md
