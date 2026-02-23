"""SessionData dataclass — session persistence payload."""

from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional

from . import session_ids as _sid


@dataclass
class SessionData:
    """Session 持久化数据"""
    session_id: str
    agent_name: str
    model_name: str
    session_type: str
    history_messages: list  # List[Dict]
    mailbox: list
    variables: Dict[str, Any]
    created_at: str
    updated_at: str
    state: str = "active"
    archived_at: Optional[str] = None
    events: list = None  # UI events like tool calls
    timeline: list = None
    context_trace: Dict[str, Any] = None
    revision: int = 0

    def __init__(self, **kwargs):
        # Compatibility for old sessions
        self.session_id = kwargs.get("session_id")
        self.agent_name = kwargs.get("agent_name")
        self.model_name = kwargs.get("model_name")
        self.session_type = kwargs.get("session_type") or _sid.infer_session_type(self.session_id or "")
        self.history_messages = kwargs.get("history_messages", [])
        self.mailbox = kwargs.get("mailbox", [])
        self.variables = kwargs.get("variables", {})
        self.created_at = kwargs.get("created_at")
        self.updated_at = kwargs.get("updated_at")
        self.state = kwargs.get("state", "active")
        self.archived_at = kwargs.get("archived_at")
        self.events = kwargs.get("events", [])
        self.timeline = kwargs.get("timeline", kwargs.get("trajectory_events", []))
        self.context_trace = kwargs.get("context_trace", {})
        self.revision = kwargs.get("revision", 0)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionData":
        return cls(**data)
