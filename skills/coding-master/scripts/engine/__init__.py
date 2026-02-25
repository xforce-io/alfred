"""Coding engine abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class EngineResult:
    success: bool
    summary: str
    files_changed: list[str] = field(default_factory=list)
    error: str | None = None


class CodingEngine(ABC):
    @abstractmethod
    def run(
        self,
        repo_path: str,
        prompt: str,
        max_turns: int = 30,
        timeout: int = 600,
    ) -> EngineResult:
        ...
