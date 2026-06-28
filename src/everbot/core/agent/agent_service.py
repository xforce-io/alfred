"""
Agent Service

Handles agent management, status monitoring, and heartbeat operations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from ..runtime.control import get_local_status, run_heartbeat_once
from ...infra.user_data import get_user_data_manager
from .provider import get_provider_for_agent


class AgentService:
    """Service for managing agents and daemon operations."""

    def __init__(self):
        self.user_data = get_user_data_manager()

    def list_agents(self) -> list[str]:
        """List all available agents."""
        return self.user_data.list_agents()

    def get_status(self) -> Dict[str, Any]:
        """Get daemon and agent status."""
        return get_local_status(self.user_data)

    async def trigger_heartbeat(self, agent_name: str, force: bool = False) -> str:
        """
        Trigger heartbeat for an agent.

        Returns:
            task_id for tracking the heartbeat execution
        """
        task_id = f"{agent_name}:{asyncio.get_event_loop().time()}"
        await run_heartbeat_once(agent_name, force=force)
        return task_id

    async def create_agent_instance(self, agent_name: str):
        """
        Create an agent instance with proper configuration.
        """
        agent_dir = self.user_data.get_agent_dir(agent_name)

        if not agent_dir.exists():
            raise ValueError(f"Agent {agent_name} does not exist")

        agent = await get_provider_for_agent(agent_name).create_agent(agent_name, agent_dir)
        return agent
