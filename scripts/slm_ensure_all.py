#!/usr/bin/env python3
"""Bulk invoke ensure_registered for every skill in a skills dir.

Use this once to migrate pre-existing skills that never went through
publish(). Safe to re-run — ensure_registered is idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _setup_path() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def main() -> int:
    _setup_path()
    from src.everbot.core.slm.state_normalizer import ensure_registered
    from src.everbot.core.slm.version_manager import VersionManager

    p = argparse.ArgumentParser()
    p.add_argument("--skills-dir", required=True, type=Path)
    p.add_argument("--eval-dir", required=True, type=Path)
    p.add_argument("--repo-skills-dir", default=None, type=Path)
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--read-skill-dirs", default=None,
        help=(
            "Colon-separated read priority chain for layered SKILL.md lookup."
            " Example: '/path/workspace_skills:/path/user_skills:/path/repo_skills'."
            " Default = single dir = --skills-dir."
        ),
    )
    args = p.parse_args()

    read_skill_dirs = None
    if args.read_skill_dirs:
        read_skill_dirs = [Path(p) for p in args.read_skill_dirs.split(":") if p]
    vm = VersionManager(
        args.skills_dir,
        eval_base_dir=args.eval_dir,
        read_skill_dirs=read_skill_dirs,
    )

    candidate_dirs = [args.skills_dir]
    if read_skill_dirs:
        candidate_dirs = read_skill_dirs
    seen: set[str] = set()
    results = []
    for base in candidate_dirs:
        if not base.exists():
            continue
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir():
                continue
            if skill_dir.name in seen:
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue
            seen.add(skill_dir.name)
            r = ensure_registered(vm, skill_dir.name, repo_skills_dir=args.repo_skills_dir)
            results.append({
                "skill_id": r.skill_id,
                "action": r.action.value,
                "detail": r.detail,
            })

    counts = Counter(r["action"] for r in results)
    report = {
        "summary": dict(counts),
        "skills": results,
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"{r['skill_id']:<30} {r['action']:<20} {r['detail']}")
        print()
        for action, count in counts.items():
            print(f"  {action}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
