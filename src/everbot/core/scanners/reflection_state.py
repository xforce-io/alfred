"""Shared watermark state for reflection skills."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_STATE_FILENAME = ".reflection_state.json"


@dataclass
class ReflectionState:
    """Persistent watermark state for reflection skills.

    Each skill maintains an independent watermark (ISO timestamp),
    stored in {workspace_path}/.reflection_state.json.
    """

    watermarks: Dict[str, str] = field(default_factory=dict)

    def get_watermark(self, skill_name: str) -> str:
        """Get watermark for a skill. Returns empty string if not set."""
        return self.watermarks.get(skill_name, "")

    def set_watermark(self, skill_name: str, value: str) -> None:
        """Set watermark for a skill."""
        self.watermarks[skill_name] = value

    @classmethod
    def load(cls, workspace_path: Path) -> "ReflectionState":
        """Load state from disk. Returns empty state on any error."""
        state_file = Path(workspace_path) / _STATE_FILENAME
        if not state_file.exists():
            return cls()
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return cls(watermarks=data.get("watermarks", {}))
        except Exception as e:
            logger.warning("Failed to load reflection state, starting fresh: %s", e)
            return cls()

    def save(self, workspace_path: Path) -> None:
        """Atomically save state to disk."""
        state_file = Path(workspace_path) / _STATE_FILENAME
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".json.tmp")
        data = json.dumps({"watermarks": self.watermarks}, ensure_ascii=False, indent=2)
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, state_file)
        except Exception as e:
            logger.error("Failed to save reflection state: %s", e)
            # Clean up tmp on failure
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
