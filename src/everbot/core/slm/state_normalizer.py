"""State normalization for SLM per-skill files.

Inspects the 4-file state (SKILL.md, current.json, metadata.json, snapshot)
and either:
  - returns NOOP if consistent,
  - bootstraps missing files (fresh skill),
  - repairs partial state (crash / manual edit recovery),
  - flags version conflicts for escalation (per policy D1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from ._atomic_io import atomic_write_text, skill_lock
from .models import CurrentPointer, VersionMetadata, VersionStatus
from .version_manager import VersionManager, read_frontmatter_version

logger = logging.getLogger(__name__)


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


def ensure_registered(
    ver_mgr: VersionManager,
    skill_id: str,
    *,
    repo_skills_dir: Optional[Path] = None,
) -> RegistrationResult:
    """Normalize SLM state for one skill. Idempotent, concurrent-safe."""
    lock_path = ver_mgr._eval_dir(skill_id) / ".lock"
    with skill_lock(lock_path):
        return _ensure_registered_locked(ver_mgr, skill_id, repo_skills_dir)


def _ensure_registered_locked(
    ver_mgr: VersionManager,
    skill_id: str,
    repo_skills_dir: Optional[Path],
) -> RegistrationResult:
    inspector = StateInspector(ver_mgr)
    before = inspector.inspect(skill_id)

    if not before.skill_md_exists:
        return RegistrationResult(
            skill_id=skill_id,
            action=RegistrationAction.SKILL_MISSING,
            detail="SKILL.md does not exist",
            before=before,
            after=before,
        )

    # All consistent: pointer + metadata + snapshot all match SKILL.md version
    if (
        before.pointer is not None
        and before.metadata is not None
        and before.snapshot_exists
        and before.pointer.current_version == before.skill_md_version
    ):
        return RegistrationResult(
            skill_id=skill_id,
            action=RegistrationAction.NOOP,
            before=before,
            after=before,
        )

    # Fresh bootstrap path: pointer absent → write snapshot, metadata, then pointer
    if before.pointer is None:
        return _bootstrap(ver_mgr, skill_id, before, repo_skills_dir, inspector)

    # Other states (partial repair / conflict) handled in Task 4.
    raise NotImplementedError(
        f"state not yet handled for {skill_id}: "
        f"pointer={before.pointer}, metadata={before.metadata}, "
        f"snapshot={before.snapshot_exists}"
    )


def _bootstrap(
    ver_mgr: VersionManager,
    skill_id: str,
    before: FileState,
    repo_skills_dir: Optional[Path],
    inspector: StateInspector,
) -> RegistrationResult:
    version = before.skill_md_version or "baseline"
    skill_md_path = ver_mgr._skill_md(skill_id)
    skill_content = skill_md_path.read_text(encoding="utf-8")

    # D2-A: repo_baseline only if the skill also exists in repo's skills/
    repo_baseline = False
    if repo_skills_dir is not None:
        repo_candidate = repo_skills_dir / skill_id / "SKILL.md"
        repo_baseline = repo_candidate.exists()

    # D3-A: if eval_report present (unhealthy or not), populate eval_summary.
    eval_report = ver_mgr.get_eval_report(skill_id, version)
    eval_summary = None
    if eval_report is not None:
        eval_summary = {
            "critical_issue_rate": eval_report.critical_issue_rate,
            "satisfaction_score": eval_report.mean_satisfaction,
        }

    ver_dir = ver_mgr._version_dir(skill_id, version)
    ver_dir.mkdir(parents=True, exist_ok=True)

    # Write order: snapshot → metadata → pointer. If we crash between writes,
    # the missing pointer is the state our next call recognizes as "bootstrap
    # again" and retries.
    snap_path = ver_dir / "skill.md"
    atomic_write_text(snap_path, skill_content)

    meta = VersionMetadata(
        version=version,
        created_at=datetime.now(timezone.utc).isoformat(),
        status=VersionStatus.ACTIVE,
        verification_phase="full",
        eval_summary=eval_summary,
    )
    atomic_write_text(ver_dir / "metadata.json", meta.to_json())

    pointer = CurrentPointer(
        current_version=version,
        stable_version=version,
        repo_baseline=repo_baseline,
        consecutive_evolve_count=0,
    )
    ver_mgr._current_json(skill_id).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ver_mgr._current_json(skill_id), pointer.to_json())

    after = inspector.inspect(skill_id)
    logger.info(
        "SLM bootstrapped %s v%s (repo_baseline=%s, eval_summary=%s)",
        skill_id, version, repo_baseline, eval_summary is not None,
    )
    return RegistrationResult(
        skill_id=skill_id,
        action=RegistrationAction.BOOTSTRAPPED,
        detail=f"v{version} repo_baseline={repo_baseline}",
        before=before,
        after=after,
    )
