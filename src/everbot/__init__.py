"""
Alfred EverBot - Ever Running Bot

持续运行的 Agent 系统，支持心跳驱动的任务执行。
"""

__version__ = "0.1.0"

from .infra.user_data import UserDataManager
from .infra.workspace import WorkspaceLoader, WorkspaceInstructions
from .core.session.session import SessionManager, SessionData
from .core.runtime.heartbeat import HeartbeatRunner
from .cli.daemon import EverBotDaemon
from .core.agent.factory import AgentFactory, get_agent_factory, create_agent
from .core.tasks.routine_manager import RoutineManager

__all__ = [
    "UserDataManager",
    "WorkspaceLoader",
    "WorkspaceInstructions",
    "SessionManager",
    "SessionData",
    "HeartbeatRunner",
    "EverBotDaemon",
    "AgentFactory",
    "get_agent_factory",
    "create_agent",
    "RoutineManager",
]
