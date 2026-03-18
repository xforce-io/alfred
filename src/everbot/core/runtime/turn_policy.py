"""Turn execution policy definitions and config-aware factories.

Defines :class:`TurnEventType`, :class:`TurnEvent`, and :class:`TurnPolicy`
data types used by the orchestrator, plus preset policies and
``build_*_policy()`` factory functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

class TurnEventType(str, Enum):
    LLM_DELTA = "llm_delta"
    LLM_ROUND_RESET = "llm_round_reset"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    SKILL = "skill"
    STATUS = "status"
    TURN_COMPLETE = "turn_complete"
    TURN_ERROR = "turn_error"


@dataclass
class TurnEvent:
    """Normalised event emitted by :meth:`TurnOrchestrator.run_turn`."""

    type: TurnEventType
    # Content fields (only relevant subset populated per type)
    content: str = ""
    tool_name: str = ""
    tool_args: str = ""
    tool_output: str = ""
    skill_name: str = ""
    skill_args: str = ""
    skill_output: str = ""
    pid: str = ""
    status: str = ""
    reference_id: str = ""
    error: str = ""
    # Truncation metadata
    args_truncated: bool = False
    args_total_chars: int = 0
    output_truncated: bool = False
    output_total_chars: int = 0
    # Aggregated answer so far (populated on TURN_COMPLETE)
    answer: str = ""
    # Stats (populated on TURN_COMPLETE / TURN_ERROR)
    tool_call_count: int = 0
    tool_execution_count: int = 0
    tool_names_executed: List[str] = field(default_factory=list)
    failed_tool_outputs: int = 0
    output_tokens: int = 0


@dataclass
class TurnPolicy:
    """Configurable knobs consumed by the orchestrator."""

    max_attempts: int = 3
    max_tool_calls: int = 50
    max_failed_tool_outputs: int = 6
    max_same_failure_signature: int = 4
    max_same_tool_intent: int = 6
    max_same_readonly_intent: int = 10
    max_consecutive_empty_llm_rounds: int = 3
    max_consecutive_think_only_rounds: Optional[int] = None  # defaults to max_tool_calls // 2
    max_consecutive_similar_llm_rounds: int = 4
    max_non_progress_events: int = 500
    max_tool_args_preview_chars: int = 500
    max_tool_output_preview_chars: int = 8000
    timeout_seconds: Optional[float] = None
    drain_extra_seconds: Optional[float] = 300
    retryable_markers: List[str] = field(default_factory=lambda: [
        "incomplete chunked read",
        "peer closed connection",
        "connecterror",
        "connection error",
        "apiconnectionerror",
        "timeout",
        "remote disconnected",
        "connection broken",
        "run_coroutine_failed",
    ])
    # Alias for max_same_failure_signature (used by quota-error detection).
    repeated_failure_limit: Optional[int] = None
    # Internal helper tools excluded from tool-call budget counting.
    budget_exempt_tools: frozenset = field(default_factory=frozenset)
    # Directory patterns to exclude from grep-like tool searches.
    grep_exclude_patterns: List[str] = field(default_factory=lambda: [
        ".venv", "node_modules", "__pycache__", ".git", "site-packages",
    ])


# ---------------------------------------------------------------------------
# Convenience presets
# ---------------------------------------------------------------------------

CHAT_POLICY = TurnPolicy(
    max_tool_calls=100,
    timeout_seconds=600,
)

HEARTBEAT_POLICY = TurnPolicy(
    max_attempts=3,
    max_tool_calls=10,
    timeout_seconds=120,
)

JOB_POLICY = TurnPolicy(
    max_attempts=1,
    max_tool_calls=20,
    max_failed_tool_outputs=5,
    max_tool_output_preview_chars=12000,
    timeout_seconds=600,
)

WORKFLOW_POLICY = TurnPolicy(
    max_attempts=2,
    max_tool_calls=60,
    max_failed_tool_outputs=8,
    max_tool_output_preview_chars=12000,
    timeout_seconds=300,
)


# ---------------------------------------------------------------------------
# Config-aware policy factories
# ---------------------------------------------------------------------------

_POLICY_DEFAULTS: Dict[str, TurnPolicy] = {
    "chat": CHAT_POLICY,
    "heartbeat": HEARTBEAT_POLICY,
    "job": JOB_POLICY,
    "workflow": WORKFLOW_POLICY,
}


def _resolve_timeout(
    policy_key: str,
    config: Optional[Dict] = None,
    agent_name: Optional[str] = None,
) -> float:
    """Resolve timeout_seconds with priority: agent-level > global > hardcoded.

    Config paths checked:
      - ``everbot.agents.<agent_name>.turn_timeout.<policy_key>``
      - ``everbot.runtime.turn_timeout.<policy_key>``
      - hardcoded default from ``_POLICY_DEFAULTS[policy_key]``
    """
    default = _POLICY_DEFAULTS[policy_key].timeout_seconds
    if config is None:
        return default

    everbot = config.get("everbot", {})
    if not isinstance(everbot, dict):
        return default

    # Global override
    timeout = (everbot.get("runtime", {})
               .get("turn_timeout", {})
               .get(policy_key))

    # Per-agent override (takes precedence)
    if agent_name:
        agent_timeout = (everbot.get("agents", {})
                         .get(agent_name, {})
                         .get("turn_timeout", {})
                         .get(policy_key))
        if agent_timeout is not None:
            timeout = agent_timeout

    if timeout is not None:
        return float(timeout)
    return default


def build_chat_policy(
    config: Optional[Dict] = None,
    agent_name: Optional[str] = None,
) -> TurnPolicy:
    """Build a chat TurnPolicy with optional config overrides."""
    timeout = _resolve_timeout("chat", config, agent_name)
    return TurnPolicy(
        max_tool_calls=CHAT_POLICY.max_tool_calls,
        timeout_seconds=timeout,
    )


def build_heartbeat_policy(
    config: Optional[Dict] = None,
    agent_name: Optional[str] = None,
) -> TurnPolicy:
    """Build a heartbeat TurnPolicy with optional config overrides."""
    timeout = _resolve_timeout("heartbeat", config, agent_name)
    return TurnPolicy(
        max_attempts=HEARTBEAT_POLICY.max_attempts,
        max_tool_calls=HEARTBEAT_POLICY.max_tool_calls,
        timeout_seconds=timeout,
    )


def build_job_policy(
    config: Optional[Dict] = None,
    agent_name: Optional[str] = None,
) -> TurnPolicy:
    """Build a job TurnPolicy with optional config overrides."""
    timeout = _resolve_timeout("job", config, agent_name)
    return TurnPolicy(
        max_attempts=JOB_POLICY.max_attempts,
        max_tool_calls=JOB_POLICY.max_tool_calls,
        max_failed_tool_outputs=JOB_POLICY.max_failed_tool_outputs,
        max_tool_output_preview_chars=JOB_POLICY.max_tool_output_preview_chars,
        timeout_seconds=timeout,
    )


def build_workflow_policy(
    config: Optional[Dict] = None,
    agent_name: Optional[str] = None,
) -> TurnPolicy:
    """Build a workflow TurnPolicy with optional config overrides."""
    timeout = _resolve_timeout("workflow", config, agent_name)
    return TurnPolicy(
        max_attempts=WORKFLOW_POLICY.max_attempts,
        max_tool_calls=WORKFLOW_POLICY.max_tool_calls,
        max_failed_tool_outputs=WORKFLOW_POLICY.max_failed_tool_outputs,
        max_tool_output_preview_chars=WORKFLOW_POLICY.max_tool_output_preview_chars,
        timeout_seconds=timeout,
    )
