"""Structured outcomes returned by built-in background jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class JobOutcome:
    """A machine-readable job terminal, separate from user-visible output."""

    status: Literal["completed", "skipped", "degraded"]
    reason: str
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
