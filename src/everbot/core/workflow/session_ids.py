"""Workflow session ID helpers."""

import uuid
from datetime import datetime


def create_workflow_session_id(agent_name: str, workflow_name: str) -> str:
    """Create a unique workflow session ID.

    Pattern: workflow_{agent}_{workflow}_{timestamp}_{uuid8}
    """
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"workflow_{agent_name}_{workflow_name}_{ts}_{short}"
