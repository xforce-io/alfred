"""ReflectionManager — heartbeat reflection phase logic."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Any, Dict
import logging

logger = logging.getLogger(__name__)


class ReflectionManager:
    """Manages heartbeat reflection: file-change detection, routine proposal extraction."""

    def __init__(self, workspace_path: Path, force_interval: timedelta):
        self.workspace_path = Path(workspace_path)
        self._force_interval = force_interval
        self.last_reflect_at: Optional[datetime] = None
        self.last_reflect_file_hashes: Dict[str, str] = {}

    def compute_file_hashes(self) -> Dict[str, str]:
        """Compute MD5 hashes for MEMORY.md and HEARTBEAT.md."""
        hashes: Dict[str, str] = {}
        for name in ("MEMORY.md", "HEARTBEAT.md"):
            path = self.workspace_path / name
            try:
                if path.exists():
                    hashes[name] = hashlib.md5(path.read_bytes()).hexdigest()
                else:
                    hashes[name] = ""
            except Exception:
                hashes[name] = ""
        return hashes

    def should_skip_reflection(self) -> bool:
        """Check whether reflection can be skipped.

        Skip when MEMORY.md and HEARTBEAT.md are unchanged since last
        reflect AND the force interval has not elapsed.
        """
        now = datetime.now()
        current_hashes = self.compute_file_hashes()

        # Force reflect if we've never reflected or force interval elapsed
        if self.last_reflect_at is None:
            return False
        if (now - self.last_reflect_at) >= self._force_interval:
            return False

        # Skip if files haven't changed
        if current_hashes == self.last_reflect_file_hashes:
            return True

        return False

    def update_reflect_state(self) -> None:
        """Record state after a successful reflect LLM call."""
        self.last_reflect_at = datetime.now()
        self.last_reflect_file_hashes = self.compute_file_hashes()

    @staticmethod
    def extract_routine_proposals(response: str) -> list[dict[str, Any]]:
        """Extract routine proposals from reflection response JSON payload."""
        if not isinstance(response, str) or not response.strip():
            return []

        def _from_payload(payload: Any) -> list[dict[str, Any]]:
            if not isinstance(payload, dict):
                return []
            routines = payload.get("routines")
            if not isinstance(routines, list):
                return []
            return [item for item in routines if isinstance(item, dict)]

        for match in re.finditer(r"```json\s*(\{.*?\})\s*```", response, re.DOTALL):
            try:
                payload = json.loads(match.group(1))
            except Exception:
                continue
            proposals = _from_payload(payload)
            if proposals:
                return proposals

        try:
            payload = json.loads(response.strip())
        except Exception:
            return []
        return _from_payload(payload)

    @staticmethod
    def normalize_routine(item: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Normalize one routine proposal into RoutineManager add payload."""
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        execution_mode = str(item.get("execution_mode") or "auto").strip().lower()
        if execution_mode not in {"inline", "isolated", "auto"}:
            execution_mode = "auto"
        description = str(item.get("description") or "").strip()
        schedule_raw = item.get("schedule")
        schedule = None
        if schedule_raw is not None:
            schedule_text = str(schedule_raw).strip()
            schedule = schedule_text or None
        timezone_name = item.get("timezone")
        if timezone_name is not None:
            timezone_name = str(timezone_name).strip() or None
        timeout_seconds = item.get("timeout_seconds", 120)
        try:
            timeout_seconds = max(1, int(timeout_seconds))
        except Exception:
            timeout_seconds = 120
        return {
            "title": title,
            "description": description,
            "schedule": schedule,
            "execution_mode": execution_mode,
            "timezone_name": timezone_name,
            "timeout_seconds": timeout_seconds,
            "source": "heartbeat_reflect",
            "allow_duplicate": False,
        }

    def apply_routine_proposals(
        self,
        response: str,
        run_id: str,
        agent_name: str,
        routine_manager: Any,
        read_heartbeat_md: Any,
    ) -> str:
        """Apply reflection-proposed routines through framework-side strong constraints.

        Args:
            response: LLM reflection response text.
            run_id: Current heartbeat run id.
            agent_name: Agent name for logging.
            routine_manager: RoutineManager instance.
            read_heartbeat_md: Callable to refresh in-memory task snapshot.
        """
        proposals = self.extract_routine_proposals(response)
        if not proposals:
            return response

        added: list[dict[str, Any]] = []
        skipped_duplicates = 0
        failed = 0

        for raw in proposals:
            normalized = self.normalize_routine(raw)
            if normalized is None:
                continue
            try:
                created = routine_manager.add_routine(**normalized)
                added.append(created)
            except ValueError as exc:
                detail = str(exc)
                if "duplicate routine" in detail or "task_id already exists" in detail:
                    skipped_duplicates += 1
                else:
                    failed += 1
                    logger.warning(
                        "Reflection routine apply rejected: agent=%s run_id=%s title=%s reason=%s",
                        agent_name,
                        run_id,
                        normalized.get("title", ""),
                        detail,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Reflection routine apply failed: agent=%s run_id=%s title=%s error=%s",
                    agent_name,
                    run_id,
                    normalized.get("title", ""),
                    str(exc),
                )

        if not added:
            return response

        # Refresh in-memory task snapshot after out-of-band task file updates.
        read_heartbeat_md()

        lines = [f"Registered {len(added)} routine(s) from heartbeat reflection."]
        for item in added[:5]:
            title = str(item.get("title") or "")
            schedule = str(item.get("schedule") or "manual")
            lines.append(f"- {title} ({schedule})")
        if skipped_duplicates > 0:
            lines.append(f"Skipped duplicates: {skipped_duplicates}.")
        if failed > 0:
            lines.append(f"Failed to apply: {failed}.")
        return "\n".join(lines)
