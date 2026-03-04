"""Base scanner interface for heartbeat gate-based change detection."""

from dataclasses import dataclass
from typing import Any


@dataclass
class ScanResult:
    """Result of a scanner check."""

    has_changes: bool  # Whether there are substantive changes
    change_summary: str  # Short description for event logging
    payload: Any = None  # Actual data, only meaningful when has_changes=True


class BaseScanner:
    """Abstract base class for all scanners.

    Scanners are lightweight gate components that detect changes
    before triggering heavier skill execution.
    """

    def check(self, watermark: str, agent_name: str = "") -> ScanResult:
        """Lightweight pre-check for changes since watermark.

        Args:
            watermark: ISO timestamp of last processed point.
            agent_name: Optional agent name for scoping.

        Returns:
            ScanResult indicating whether changes exist.
        """
        raise NotImplementedError
