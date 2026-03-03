"""Workflow data models: configs, state, events, results, report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union


# ---------------------------------------------------------------------------
# Configuration models (from YAML)
# ---------------------------------------------------------------------------

@dataclass
class VerificationCmdConfig:
    """External verification command configuration."""

    cmd: str
    timeout_seconds: int = 120
    working_dir: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class PhaseConfig:
    """Single phase configuration."""

    name: str
    # Mode A: LLM-driven
    instruction_ref: Optional[str] = None
    max_turns: int = 10
    max_tool_calls: int = 50
    timeout_seconds: int = 300
    checkpoint: bool = False
    completion_signal: str = "llm_decision"
    input_artifacts: List[str] = field(default_factory=list)
    allowed_tools: Optional[List[str]] = None
    on_failure: str = "abort"
    max_retries: int = 1
    # Mode B: command verification
    verification_cmd: Optional[VerificationCmdConfig] = None
    # LLM verify protocol
    verify_protocol: Optional[str] = None


@dataclass
class PhaseGroupConfig:
    """PhaseGroup configuration: action + verify loop."""

    name: str
    action_phase: str
    verify_phase: str
    setup_phase: Optional[str] = None
    max_iterations: int = 5
    phases: List[PhaseConfig] = field(default_factory=list)
    on_exhausted: str = "rollback"
    rollback_target: Optional[str] = None


@dataclass
class TaskSessionConfig:
    """Top-level workflow configuration."""

    name: str = ""
    description: str = ""
    phases: List[Union[PhaseConfig, PhaseGroupConfig]] = field(default_factory=list)
    total_timeout_seconds: int = 1800
    total_max_tool_calls: int = 200
    max_rollback_retries: int = 2


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

@dataclass
class TaskSessionState:
    """Mutable runtime state for a TaskSession."""

    session_id: str
    task_id: str = ""
    current_phase_index: int = 0
    rollback_retry_count: int = 0
    status: str = "pending"
    artifacts: Dict[str, str] = field(default_factory=dict)
    total_tool_calls_used: int = 0
    start_time: Optional[datetime] = None
    git_start_commit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "current_phase_index": self.current_phase_index,
            "rollback_retry_count": self.rollback_retry_count,
            "status": self.status,
            "artifacts": dict(self.artifacts),
            "total_tool_calls_used": self.total_tool_calls_used,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "git_start_commit": self.git_start_commit,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSessionState":
        start_time = data.get("start_time")
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time)
        return cls(
            session_id=data["session_id"],
            task_id=data.get("task_id", ""),
            current_phase_index=data.get("current_phase_index", 0),
            rollback_retry_count=data.get("rollback_retry_count", 0),
            status=data.get("status", "pending"),
            artifacts=data.get("artifacts", {}),
            total_tool_calls_used=data.get("total_tool_calls_used", 0),
            start_time=start_time,
            git_start_commit=data.get("git_start_commit"),
        )


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------

@dataclass
class CmdResult:
    """Result of a verification command execution."""

    exit_code: int
    output: str


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class TaskSessionEvent:
    """Event emitted during workflow execution."""

    event_type: str  # phase_start, phase_complete, verify_pass, verify_fail,
                     # rollback, checkpoint, workflow_complete, workflow_failed,
                     # budget_warning
    session_id: str = ""
    phase_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Phase result
# ---------------------------------------------------------------------------

@dataclass
class PhaseResult:
    """Result of a single phase execution."""

    artifact: str = ""
    tool_calls_used: int = 0
    full_response: str = ""
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------

@dataclass
class VerifyTraceEntry:
    """One verification attempt within a PhaseGroup."""

    iteration: int
    passed: bool
    exit_code: Optional[int] = None
    output: str = ""
    duration_seconds: float = 0.0


@dataclass
class PhaseTraceEntry:
    """Execution trace for one phase."""

    phase_name: str
    phase_type: str = "phase"  # phase | phase_group | verification_cmd
    status: str = ""  # completed | exceeded | failed | skipped
    artifact_preview: str = ""
    tool_calls_used: int = 0
    duration_seconds: float = 0.0
    verify_traces: List[VerifyTraceEntry] = field(default_factory=list)
    iterations: int = 0
    rollback_triggered: bool = False


@dataclass
class WorkflowReport:
    """Final workflow completion report."""

    session_id: str
    workflow_name: str = ""
    status: str = ""  # done | failed | cancelled
    total_duration_seconds: float = 0.0
    total_tool_calls: int = 0
    phase_traces: List[PhaseTraceEntry] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    final_artifact: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workflow_name": self.workflow_name,
            "status": self.status,
            "total_duration_seconds": self.total_duration_seconds,
            "total_tool_calls": self.total_tool_calls,
            "phase_traces": [
                {
                    "phase_name": t.phase_name,
                    "phase_type": t.phase_type,
                    "status": t.status,
                    "artifact_preview": t.artifact_preview,
                    "tool_calls_used": t.tool_calls_used,
                    "duration_seconds": t.duration_seconds,
                    "verify_traces": [
                        {
                            "iteration": v.iteration,
                            "passed": v.passed,
                            "exit_code": v.exit_code,
                            "output": v.output,
                            "duration_seconds": v.duration_seconds,
                        }
                        for v in t.verify_traces
                    ],
                    "iterations": t.iterations,
                    "rollback_triggered": t.rollback_triggered,
                }
                for t in self.phase_traces
            ],
            "files_modified": self.files_modified,
            "final_artifact": self.final_artifact,
            "error": self.error,
        }
