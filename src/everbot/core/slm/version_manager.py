"""Version manager — publish, rollback, and inspect skill versions.

All version data lives under ``~/.alfred/skills/{skill_id}/.eval/``.
The runtime only reads ``SKILL.md``; ``.eval/`` is purely offline.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import CurrentPointer, EvalReport, VersionMetadata, VersionStatus

logger = logging.getLogger(__name__)


def read_frontmatter_version(skill_md_path: Path) -> str:
    """Extract version from SKILL.md frontmatter. Returns 'baseline' if absent.

    Handles binary/non-UTF8 SKILL.md gracefully by falling back to 'baseline'
    instead of propagating UnicodeDecodeError.
    """
    if not skill_md_path.exists():
        return "baseline"
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return "baseline"
    match = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return "baseline"
    for line in match.group(1).splitlines():
        m = re.match(r'version:\s*["\']?([^"\']+)["\']?', line.strip())
        if m:
            return m.group(1).strip()
    return "baseline"


class VersionManager:
    """Manage skill versions and per-agent evaluation data.

    Concurrency invariant: publish/rollback/activate write several files
    without per-call locking. Callers must hold the per-skill file lock
    (slm._atomic_io.skill_lock on {eval_dir}/.lock) for the duration of
    any write call. Today the only writer paths are ensure_registered
    and _post_evaluate, both of which acquire the lock. Adding a new
    caller — e.g. a CLI improver, an HTTP admin endpoint — without
    acquiring the lock will race against them.

    Directory layout (per-agent eval)::

        ~/.alfred/agents/{agent}/skill_eval/{skill_id}/
          current.json           <- pointer: current + stable version
          versions/
            v1.0/
              skill.md           <- snapshot
              metadata.json
              eval_report.json   <- optional

    When *eval_base_dir* is ``None``, falls back to the legacy layout
    ``skills_dir/{skill_id}/.eval/`` for backward compatibility.
    """

    def __init__(
        self,
        skills_dir: Path,
        eval_base_dir: Optional[Path] = None,
        read_skill_dirs: Optional[List[Path]] = None,
    ) -> None:
        """
        Args:
            skills_dir: Writable skills directory. SLM publish/rollback writes
                here. For agent workspaces this is layer 0 of the loader chain.
            eval_base_dir: Per-agent eval directory.
            read_skill_dirs: Read priority chain (highest first). Used by
                _resolve_skill_md to find baseline SKILL.md content across
                layers. Defaults to ``[skills_dir]`` for backward compat —
                old callers see exactly the previous behavior.
        """
        self._skills_dir = skills_dir
        self._eval_base_dir = eval_base_dir
        self._read_skill_dirs: List[Path] = (
            list(read_skill_dirs) if read_skill_dirs else [skills_dir]
        )

    def _skill_dir(self, skill_id: str) -> Path:
        return self._skills_dir / skill_id

    def _eval_dir(self, skill_id: str) -> Path:
        if self._eval_base_dir is not None:
            return self._eval_base_dir / skill_id
        return self._skill_dir(skill_id) / ".eval"

    def _versions_dir(self, skill_id: str) -> Path:
        return self._eval_dir(skill_id) / "versions"

    def _version_dir(self, skill_id: str, version: str) -> Path:
        return self._versions_dir(skill_id) / f"v{version}"

    def _current_json(self, skill_id: str) -> Path:
        return self._eval_dir(skill_id) / "current.json"

    def _skill_md(self, skill_id: str) -> Path:
        return self._skill_dir(skill_id) / "SKILL.md"

    def _resolve_skill_md(self, skill_id: str) -> Path:
        """Find the live SKILL.md by walking read dirs in priority order.

        Returns the highest-priority path that exists. If no layer has the
        file, returns the writable path (caller can decide whether to
        treat that as 'missing' or write content there).
        """
        for d in self._read_skill_dirs:
            candidate = d / skill_id / "SKILL.md"
            if candidate.exists():
                return candidate
        return self._skill_md(skill_id)

    def is_symlink_managed(self, skill_id: str) -> bool:
        """Return True if this skill's user dir or SKILL.md path traverses a
        symlink (e.g. ``~/.alfred/skills/foo/`` is symlinked to a repo dir).

        SLM cannot safely write or unlink through such a path: ``Path.unlink``
        and ``Path.write_text`` follow symlinks, so they would mutate the
        upstream target rather than a per-user override. Both publish() and
        rollback() must abort on symlink-managed skills to avoid corrupting
        upstream files.
        """
        skill_dir = self._skill_dir(skill_id)
        if skill_dir.is_symlink():
            return True
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_symlink():
            return True
        return False

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_pointer(self, skill_id: str) -> Optional[CurrentPointer]:
        """Read current.json pointer. Returns None if not managed by SLM."""
        path = self._current_json(skill_id)
        if not path.exists():
            return None
        try:
            return CurrentPointer.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Malformed current.json for %s: %s", skill_id, e)
            return None

    def get_metadata(self, skill_id: str, version: str) -> Optional[VersionMetadata]:
        """Read metadata for a specific version."""
        path = self._version_dir(skill_id, version) / "metadata.json"
        if not path.exists():
            return None
        try:
            return VersionMetadata.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Malformed metadata for %s v%s: %s", skill_id, version, e)
            return None

    def get_eval_report(self, skill_id: str, version: str) -> Optional[EvalReport]:
        """Read evaluation report for a specific version."""
        path = self._version_dir(skill_id, version) / "eval_report.json"
        if not path.exists():
            return None
        try:
            return EvalReport.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Malformed eval_report for %s v%s: %s", skill_id, version, e)
            return None

    def list_versions(self, skill_id: str) -> List[str]:
        """List all version numbers for a skill, sorted."""
        vdir = self._versions_dir(skill_id)
        if not vdir.exists():
            return []
        versions = []
        for p in vdir.iterdir():
            if p.is_dir() and p.name.startswith("v"):
                versions.append(p.name[1:])  # strip "v" prefix
        return sorted(versions)

    def get_active_version(self, skill_id: str) -> str:
        """Get the version currently in SKILL.md (from frontmatter)."""
        return read_frontmatter_version(self._skill_md(skill_id))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def publish(self, skill_id: str, version: str, skill_content: str) -> None:
        """Publish a new version: write SKILL.md + create version snapshot.

        Steps:
        1. Write SKILL.md to ~/.alfred/skills/{skill_id}/
        2. Snapshot to .eval/versions/v{version}/
        3. Update current.json
        4. Set previous active version as stable (if exists)

        Refuses to operate on symlink-managed skills: writing to the live
        SKILL.md would resolve through the symlink and overwrite the
        upstream repo file. Caller (typically _maybe_evolve) must treat
        this as evolve-failed.
        """
        if self.is_symlink_managed(skill_id):
            raise ValueError(
                f"{skill_id} is symlink-managed; publish would overwrite upstream. "
                "SLM evolve is disabled for symlinked skills — convert the install "
                "to a real directory to enable evolution."
            )

        skill_dir = self._skill_dir(skill_id)
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Determine previous pointer for stable promotion
        old_pointer = self.get_pointer(skill_id)

        # 1. Write SKILL.md
        skill_md = self._skill_md(skill_id)
        skill_md.write_text(skill_content, encoding="utf-8")

        # 2. Snapshot
        ver_dir = self._version_dir(skill_id, version)
        ver_dir.mkdir(parents=True, exist_ok=True)
        (ver_dir / "skill.md").write_text(skill_content, encoding="utf-8")

        meta = VersionMetadata(
            version=version,
            created_at=datetime.now(timezone.utc).isoformat(),
            status=VersionStatus.TESTING,
        )
        (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")

        # 3. Update pointer
        stable = old_pointer.current_version if old_pointer else ""
        repo_baseline = not bool(stable)
        pointer = CurrentPointer(
            current_version=version,
            stable_version=stable,
            repo_baseline=repo_baseline,
        )
        self._current_json(skill_id).parent.mkdir(parents=True, exist_ok=True)
        self._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")

        # 4. Promote previous current to stable (mark active)
        if old_pointer and old_pointer.current_version:
            old_meta = self.get_metadata(skill_id, old_pointer.current_version)
            if old_meta and old_meta.status == VersionStatus.TESTING:
                old_meta.status = VersionStatus.ACTIVE
                old_meta_path = self._version_dir(skill_id, old_pointer.current_version) / "metadata.json"
                old_meta_path.write_text(old_meta.to_json(), encoding="utf-8")

        logger.info("Published %s v%s", skill_id, version)

    def save_eval_report(self, skill_id: str, version: str, report: EvalReport) -> None:
        """Save evaluation report and update version metadata summary."""
        ver_dir = self._version_dir(skill_id, version)
        ver_dir.mkdir(parents=True, exist_ok=True)
        (ver_dir / "eval_report.json").write_text(report.to_json(), encoding="utf-8")

        # Update metadata summary
        meta = self.get_metadata(skill_id, version)
        if meta:
            meta.eval_summary = {
                "critical_issue_rate": report.critical_issue_rate,
                "satisfaction_score": report.mean_satisfaction,
            }
            (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")

    def activate(self, skill_id: str, version: str) -> None:
        """Mark a version as active (passed is_promotable evaluation)."""
        meta = self.get_metadata(skill_id, version)
        if not meta:
            raise ValueError(f"Version {version} not found for {skill_id}")
        meta.status = VersionStatus.ACTIVE
        ver_dir = self._version_dir(skill_id, version)
        (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")

        # Update pointer: this version becomes stable
        pointer = self.get_pointer(skill_id)
        if pointer:
            pointer.stable_version = version
            pointer.repo_baseline = False
            pointer.consecutive_evolve_count = 0
            self._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")

        logger.info("Activated %s v%s", skill_id, version)

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self, skill_id: str, reason: str = "") -> str:
        """Rollback to the stable version.

        Returns the version rolled back to, or raises ValueError.

        Refuses to operate on symlink-managed skills: both branches below
        would resolve through the symlink and mutate upstream files (an
        unlink would destroy the upstream SKILL.md; a write_text would
        overwrite upstream with snapshot content). Caller must surface the
        error so the operator can switch the install layout if SLM evolve
        is required for that skill.
        """
        if self.is_symlink_managed(skill_id):
            raise ValueError(
                f"{skill_id} is symlink-managed; rollback would mutate upstream. "
                "Convert the install to a real directory (copy SKILL.md instead "
                "of symlinking) to enable SLM rollback/evolve."
            )

        pointer = self.get_pointer(skill_id)
        if not pointer:
            raise ValueError(f"No SLM pointer for {skill_id}, nothing to rollback")

        current = pointer.current_version
        if not current:
            raise ValueError(f"No current version for {skill_id}")

        # Suspend the current version
        cur_meta = self.get_metadata(skill_id, current)
        if cur_meta:
            cur_meta.status = VersionStatus.SUSPENDED
            cur_meta.suspended_reason = reason
            cur_meta_path = self._version_dir(skill_id, current) / "metadata.json"
            cur_meta_path.write_text(cur_meta.to_json(), encoding="utf-8")

        # Rollback
        if pointer.repo_baseline:
            # Delete override → loader falls back to repo baseline
            skill_md = self._skill_md(skill_id)
            if skill_md.exists():
                skill_md.unlink()
            rolled_to = "baseline"
        else:
            stable = pointer.stable_version
            if not stable:
                raise ValueError(f"No stable version for {skill_id}")
            snapshot = self._version_dir(skill_id, stable) / "skill.md"
            if not snapshot.exists():
                raise ValueError(f"Stable snapshot missing: {skill_id} v{stable}")
            skill_md = self._skill_md(skill_id)
            # Ensure the writable layer's <skills_dir>/<skill_id>/ exists.
            # With layered SLM, the writable dir is the agent workspace which
            # is typically empty until the first publish/rollback writes here.
            skill_md.parent.mkdir(parents=True, exist_ok=True)
            skill_md.write_text(snapshot.read_text(encoding="utf-8"), encoding="utf-8")
            rolled_to = stable

        # Update pointer
        pointer.current_version = rolled_to
        self._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")

        logger.info("Rolled back %s to %s (reason: %s)", skill_id, rolled_to, reason)
        return rolled_to

    # ------------------------------------------------------------------
    # Consistency check
    # ------------------------------------------------------------------

    def check_consistency(self, skill_id: str) -> bool:
        """Check if SKILL.md frontmatter version matches current.json.

        If pointer is missing entirely, delegate to ensure_registered which
        bootstraps the missing materials. This closes the long-standing
        blind spot where un-published skills were treated as 'not managed'.

        Returns True if was consistent (or successfully bootstrapped),
        False if fixed.
        """
        pointer = self.get_pointer(skill_id)
        if not pointer:
            # Lazy import avoids circular (state_normalizer imports VersionManager).
            # repo_skills_dir=None is the safe choice — bootstrap will set
            # repo_baseline=False, so rollback will never unlink the skill file.
            from .state_normalizer import ensure_registered, RegistrationAction
            result = ensure_registered(self, skill_id, repo_skills_dir=None)
            return result.action in (
                RegistrationAction.NOOP,
                RegistrationAction.BOOTSTRAPPED,
                RegistrationAction.SKILL_MISSING,
            )

        actual = self.get_active_version(skill_id)
        if actual == pointer.current_version:
            return True

        logger.warning(
            "Inconsistency detected for %s: SKILL.md=%s, pointer=%s. Fixing pointer.",
            skill_id, actual, pointer.current_version,
        )
        pointer.current_version = actual
        self._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")
        return False
