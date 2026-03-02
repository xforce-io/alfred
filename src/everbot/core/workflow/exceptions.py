"""Workflow exception hierarchy."""

from __future__ import annotations

from typing import List, Optional


class WorkflowError(Exception):
    """Base exception for all workflow errors."""


class ConfigValidationError(WorkflowError):
    """YAML validation failure at load time."""

    def __init__(self, message: str, *, path: str = ""):
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class PhaseGroupExhaustedError(WorkflowError):
    """max_iterations reached without verification pass."""

    def __init__(
        self,
        *,
        group: str,
        iterations: int,
        failure_summary: str,
        failure_history: List[str],
    ):
        self.group = group
        self.iterations = iterations
        self.failure_summary = failure_summary
        self.failure_history = failure_history
        super().__init__(
            f"PhaseGroup '{group}' exhausted after {iterations} iterations"
        )


class BudgetExhaustedError(WorkflowError):
    """Total tool or time budget exceeded."""

    def __init__(
        self,
        *,
        budget_type: str,
        used: int,
        limit: int,
    ):
        self.budget_type = budget_type
        self.used = used
        self.limit = limit
        super().__init__(
            f"Budget exhausted: {budget_type} used={used}, limit={limit}"
        )


class CheckpointPauseError(WorkflowError):
    """Workflow should pause for review."""

    def __init__(
        self,
        *,
        phase_name: str,
        artifact: Optional[str] = None,
    ):
        self.phase_name = phase_name
        self.artifact = artifact
        super().__init__(f"Checkpoint pause at phase '{phase_name}'")
