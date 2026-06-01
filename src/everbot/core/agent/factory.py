"""Backward-compat shim.

The real ``AgentFactory`` now lives in
``everbot.core.agent.provider.dolphin.factory``.  Existing imports
(``from ...core.agent.factory import AgentFactory/create_agent/get_agent_factory``)
keep working through this re-export, including the class's static helper methods
used across cron/heartbeat/core_service/cli.
"""
from .provider.dolphin.factory import (  # noqa: F401
    AgentFactory,
    create_agent,
    get_agent_factory,
)

__all__ = ["AgentFactory", "create_agent", "get_agent_factory"]
