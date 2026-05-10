#!/usr/bin/env python3
"""skill-evolver commit step.

Validates the rewritten SKILL.md content and publishes it as a new testing
version via VersionManager. Concurrent auto evolves serialize via skill_lock.
"""
from __future__ import annotations

import re


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_VERSION_LINE_RE = re.compile(r'^\s*version\s*:\s*["\']?([^"\'\n]+?)["\']?\s*$', re.MULTILINE)


def _validate_content(content: str, *, expected_version: str) -> None:
    """Raise ValueError unless content has well-formed frontmatter whose
    version matches expected_version exactly."""
    fm_match = _FRONTMATTER_RE.match(content)
    if not fm_match:
        raise ValueError("missing frontmatter (file must start with '---' block)")
    fm_body = fm_match.group(1)
    ver_match = _VERSION_LINE_RE.search(fm_body)
    if not ver_match:
        raise ValueError("frontmatter is missing the 'version:' field")
    actual = ver_match.group(1).strip()
    if actual != expected_version:
        raise ValueError(
            f"frontmatter version mismatch: file has '{actual}', expected '{expected_version}'"
        )


import argparse
import json
import sys
from pathlib import Path


def _setup_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _err(msg: str, code: int = 1) -> None:
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="skill-evolver commit step")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--content-file", required=True)
    args = parser.parse_args(argv)

    _setup_import_path()
    from src.everbot.infra.user_data import get_user_data_manager
    from src.everbot.core.slm.version_manager import VersionManager
    from src.everbot.core.slm._atomic_io import skill_lock

    content_path = Path(args.content_file)
    if not content_path.exists():
        _err(f"content file not found: {content_path}")
    content = content_path.read_text(encoding="utf-8")

    try:
        _validate_content(content, expected_version=args.version)
    except ValueError as e:
        _err(str(e))

    workspace = Path(args.workspace).resolve()
    agent_name = workspace.name
    udm = get_user_data_manager()
    writable = udm.get_agent_writable_skills_dir(agent_name)
    eval_base = udm.get_agent_skill_eval_dir(agent_name)
    read_dirs = udm.get_agent_read_skill_dirs(agent_name)

    vm = VersionManager(
        skills_dir=writable,
        eval_base_dir=eval_base,
        read_skill_dirs=read_dirs,
    )

    lock_path = eval_base / args.skill / ".lock"
    with skill_lock(lock_path):
        try:
            # VersionManager.publish constructs a fresh CurrentPointer, which
            # implicitly resets consecutive_evolve_count to 0 — intentional for
            # user-directed evolve (explicit user intent shouldn't inherit the
            # auto-evolve failure history).
            vm.publish(args.skill, args.version, content)
        except Exception as e:
            _err(f"publish failed: {e}")
        pointer = vm.get_pointer(args.skill)

    print(json.dumps({
        "status": "ok",
        "skill": args.skill,
        "version": args.version,
        "current_pointer": pointer.current_version if pointer else "",
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
