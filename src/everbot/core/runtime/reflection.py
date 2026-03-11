"""ReflectionManager — heartbeat reflection phase logic."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Any, Dict, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class ParsedReflectionResponse:
    """Parsed unified reflection response.

    Expected LLM output format:
    {
        "heartbeat_ok": bool,
        "push_message": str | null,
        "routines": [...] | null
    }
    """

    heartbeat_ok: bool = True
    push_message: Optional[str] = None
    routines: Optional[List[Dict[str, Any]]] = None
    raw_response: str = ""

    def __post_init__(self):
        if self.routines is None:
            self.routines = []


class ReflectionManager:
    """Manages heartbeat reflection: file-change detection, routine proposal extraction."""

    def __init__(self, workspace_path: Path, force_interval: timedelta):
        self.workspace_path = Path(workspace_path)
        self._force_interval = force_interval
        self.last_reflect_at: Optional[datetime] = None
        self.last_reflect_file_hashes: Dict[str, str] = {}

    def compute_file_hashes(self) -> Dict[str, str]:
        """Compute SHA-256 hashes for MEMORY.md and HEARTBEAT.md."""
        hashes: Dict[str, str] = {}
        for name in ("MEMORY.md", "HEARTBEAT.md"):
            path = self.workspace_path / name
            try:
                if path.exists():
                    hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
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
    def _parse_heartbeat_ok(value: Any) -> bool:
        """Parse heartbeat_ok with string compatibility for LLM JSON output."""
        if isinstance(value, bool):
            return value
        if value is None:
            return True
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "ok"}:
                return True
            if normalized in {"false", "0", "no", "n", "null", "none", ""}:
                return False
        return bool(value)

    @staticmethod
    def extract_unified_response(response: str) -> ParsedReflectionResponse:
        """Parse unified reflection response format.

        Expected format:
        {
            "heartbeat_ok": bool,
            "push_message": str | null,
            "routines": [...] | null
        }

        Backward compatible: if response contains only "routines" array
        without the wrapper, it will be parsed as heartbeat_ok=true,
        push_message=null, routines=[...]
        """
        result = ParsedReflectionResponse(raw_response=response)

        if not isinstance(response, str) or not response.strip():
            return result

        json_text = None

        # Try code blocks first
        for match in re.finditer(r"```json\s*(\{.*?\})\s*```", response, re.DOTALL):
            try:
                json_text = match.group(1)
                payload = json.loads(json_text)
                # Check if this looks like unified format
                if isinstance(payload, dict) and ("heartbeat_ok" in payload or "push_message" in payload):
                    break
                json_text = None  # Keep looking if not unified format
            except Exception:
                continue

        # Try raw JSON if no unified format code block found
        if json_text is None:
            start = response.find("{")
            end = response.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    json_text = response[start : end + 1]
                    payload = json.loads(json_text)
                    if not isinstance(payload, dict):
                        json_text = None
                except Exception:
                    json_text = None

        if json_text is None:
            # No valid JSON found
            return result

        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError:
            return result

        if not isinstance(payload, dict):
            return result

        # Parse unified format fields
        result.heartbeat_ok = ReflectionManager._parse_heartbeat_ok(
            payload.get("heartbeat_ok", True)
        )

        push_msg = payload.get("push_message")
        if push_msg and isinstance(push_msg, str):
            push_msg = push_msg.strip()
            if push_msg.lower() not in ("null", "none", ""):
                result.push_message = push_msg

        routines = payload.get("routines")
        if isinstance(routines, list):
            result.routines = [item for item in routines if isinstance(item, dict)]

        return result

    @staticmethod
    def extract_routine_proposals(response: str) -> list[dict[str, Any]]:
        """Extract routine proposals from reflection response JSON payload.

        Backward compatible: works with both old format (just routines array)
        and new unified format.
        """
        # First try unified format
        unified = ReflectionManager.extract_unified_response(response)
        if unified.routines:
            return unified.routines

        # Fall back to legacy extraction
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
