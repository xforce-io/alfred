#!/usr/bin/env python3
"""skill-evolver prepare step.

Reads the target skill's current SKILL.md across the read priority chain,
generates a new userevolve-tagged version number, and emits a JSON payload
that the agent uses to drive its rewrite step.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


_SUFFIX_RE = re.compile(r"-(?:user)?evolve-\d+$")


def _extract_base(version: str) -> str:
    """Strip any -evolve-<ts> or -userevolve-<ts> suffix to recover the base."""
    return _SUFFIX_RE.sub("", version)


def _new_version(current: str, *, ts: str | None = None) -> str:
    """Compute the new userevolve version from the current version string.

    Args:
        current: The existing SKILL.md frontmatter version, possibly with a
            prior -evolve- or -userevolve- suffix.
        ts: Optional timestamp override (YYYYMMDDHHMM) — supplied by tests
            for determinism. Defaults to UTC now.
    """
    base = _extract_base(current)
    if ts is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"{base}-userevolve-{ts}"


import argparse
import json
import sys
from pathlib import Path


def _setup_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _resolve_skill_md(read_dirs: list[Path], skill_id: str) -> Path | None:
    """Walk the read priority chain; return first existing SKILL.md."""
    for d in read_dirs:
        p = d / skill_id / "SKILL.md"
        if p.exists():
            return p
    return None


def _err(msg: str, code: int = 1) -> None:
    """Emit an error JSON to stdout and exit non-zero."""
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="skill-evolver prepare step")
    parser.add_argument("--workspace", required=True, help="Agent workspace root (~/.alfred/agents/<agent>/)")
    parser.add_argument("--skill", required=True, help="Target skill id")
    args = parser.parse_args(argv)

    _setup_import_path()
    from src.everbot.infra.user_data import get_user_data_manager
    from src.everbot.core.slm.version_manager import read_frontmatter_version

    workspace = Path(args.workspace).resolve()
    agent_name = workspace.name

    udm = get_user_data_manager()
    read_dirs = udm.get_agent_read_skill_dirs(agent_name)

    skill_md = _resolve_skill_md(read_dirs, args.skill)
    if skill_md is None:
        _err(f"skill '{args.skill}' not found in any read layer for agent '{agent_name}'")

    current_content = skill_md.read_text(encoding="utf-8")
    current_version = read_frontmatter_version(skill_md)
    new_ver = _new_version(current_version)

    tmp_dir = workspace / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ts = new_ver.rsplit("-", 1)[-1]
    tmp_file = tmp_dir / f"skill-evolver-{args.skill}-{ts}.md"

    print(json.dumps({
        "current_skill_md": current_content,
        "new_version": new_ver,
        "tmp_file": str(tmp_file),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
