"""State normalization for SLM per-skill files.

Inspects the 4-file state (SKILL.md, current.json, metadata.json, snapshot)
and either:
  - returns NOOP if consistent,
  - bootstraps missing files (fresh skill),
  - repairs partial state (crash / manual edit recovery),
  - flags version conflicts for escalation (per policy D1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import CurrentPointer, VersionMetadata
from .version_manager import VersionManager, read_frontmatter_version


class RegistrationAction(str, Enum):
    NOOP = "noop"
    BOOTSTRAPPED = "bootstrapped"
    REPAIRED_METADATA = "repaired_metadata"
    REPAIRED_SNAPSHOT = "repaired_snapshot"
    CONFLICT_DETECTED = "conflict_detected"
    SKILL_MISSING = "skill_missing"


@dataclass
class FileState:
    skill_md_exists: bool
    # None iff skill_md_exists=False; "baseline" when file exists but has
    # no version key in frontmatter. Never None when skill_md_exists=True.
    skill_md_version: Optional[str]
    pointer: Optional[CurrentPointer]
    metadata: Optional[VersionMetadata]
    snapshot_exists: bool


@dataclass
class RegistrationResult:
    skill_id: str
    action: RegistrationAction
    detail: str = ""
    before: Optional[FileState] = None
    after: Optional[FileState] = None


class StateInspector:
    """Pure reader — never writes. Returns a FileState snapshot."""

    def __init__(self, ver_mgr: VersionManager) -> None:
        self._vm = ver_mgr

    def inspect(self, skill_id: str) -> FileState:
        skill_md = self._vm._skill_md(skill_id)
        skill_md_exists = skill_md.exists()
        skill_md_version = (
            read_frontmatter_version(skill_md) if skill_md_exists else None
        )
        pointer = self._vm.get_pointer(skill_id)
        metadata: Optional[VersionMetadata] = None
        snapshot_exists = False
        if pointer and pointer.current_version:
            metadata = self._vm.get_metadata(skill_id, pointer.current_version)
            snap = (
                self._vm._version_dir(skill_id, pointer.current_version)
                / "skill.md"
            )
            snapshot_exists = snap.exists()
        return FileState(
            skill_md_exists=skill_md_exists,
            skill_md_version=skill_md_version,
            pointer=pointer,
            metadata=metadata,
            snapshot_exists=snapshot_exists,
        )
