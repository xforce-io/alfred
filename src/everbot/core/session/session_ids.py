"""Pure session ID helper functions.

Extracted from SessionManager to break circular dependencies — SessionData
and SessionPersistence need ``infer_session_type`` but should not depend on
the full SessionManager class.
"""

import uuid
from datetime import datetime
from typing import Optional


def get_primary_session_id(agent_name: str) -> str:
    """Return the canonical long-lived session id for one agent."""
    return f"web_session_{agent_name}"


def infer_session_type(session_id: str) -> str:
    """Infer runtime session type from session id."""
    sid = str(session_id or "")
    if sid.startswith("heartbeat_session_"):
        return "heartbeat"
    if sid.startswith("job_"):
        return "job"
    if sid.startswith("web_session_") and "__" in sid:
        return "sub"
    if sid.startswith("web_session_"):
        return "primary"
    # Non-web channel sessions (tg_session_, discord_session_, etc.)
    from ..channel.session_resolver import ChannelSessionResolver
    for channel_type, prefix in ChannelSessionResolver._PREFIX_MAP.items():
        if channel_type == "web":
            continue
        if sid.startswith(prefix):
            return "channel"
    return "primary"


def get_heartbeat_session_id(agent_name: str) -> str:
    """Return heartbeat-only session id for one agent."""
    return f"heartbeat_session_{agent_name}"


def get_session_prefix(agent_name: str) -> str:
    """Return the session id prefix for one agent."""
    return f"web_session_{agent_name}"


def resolve_agent_name(session_id: str) -> Optional[str]:
    """Extract agent name from a session ID."""
    if session_id.startswith("web_session_"):
        # Matches web_session_{agent_name} or web_session_{agent_name}__suffix
        rem = session_id[len("web_session_"):]
        if "__" in rem:
            return rem.split("__")[0]
        return rem
    return None


def is_valid_agent_session_id(agent_name: str, session_id: str) -> bool:
    """Validate one session id belongs to the given agent namespace."""
    from .persistence import SessionPersistence
    if not SessionPersistence.is_safe_session_id(session_id):
        return False
    primary = get_primary_session_id(agent_name)
    if session_id == primary:
        return True
    if session_id.startswith(primary):
        suffix = session_id[len(primary):]
        if bool(suffix) and suffix[0] in "._-":
            return True
    # Also accept non-web channel sessions (tg_session_, discord_session_, etc.)
    from ..channel.session_resolver import ChannelSessionResolver
    for channel_type, prefix in ChannelSessionResolver._PREFIX_MAP.items():
        if channel_type == "web":
            continue
        expected = f"{prefix}{agent_name}{ChannelSessionResolver._SEP}"
        if session_id.startswith(expected):
            return True
    return False


def create_chat_session_id(agent_name: str) -> str:
    """Create a new chat session id for one agent."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{get_session_prefix(agent_name)}__{ts}_{short}"
