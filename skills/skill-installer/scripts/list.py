#!/usr/bin/env python3
"""
List installed and available skills
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

SKILLS_STATE_FILE = Path.home() / ".alfred" / "skills-state.json"


def find_skills_directories() -> List[Path]:
    """Find all skills directories"""
    home = Path.home()
    candidates = [
        Path.cwd() / "skills",
        home / ".alfred" / "skills",
    ]

    # Also check from config if available
    config_path = Path.cwd() / "config" / "dolphin.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f)
                resource_dirs = config.get("resource_skills", {}).get("directories", [])
                for d in resource_dirs:
                    path = Path(d).expanduser()
                    if path.exists():
                        candidates.append(path)
        except ImportError:
            pass
        except Exception:
            pass

    return [p for p in candidates if p.exists() and p.is_dir()]


def parse_skill_metadata(skill_dir: Path) -> Dict:
    """Parse SKILL.md frontmatter to extract metadata"""
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.exists():
        return {}

    with open(skill_md) as f:
        content = f.read()

    # Extract title (first # heading)
    title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    title = title_match.group(1) if title_match else skill_dir.name

    # Extract description (first paragraph after title)
    desc_match = re.search(r'^#\s+.+$\n\n(.+?)(?:\n\n|\n#|$)', content, re.MULTILINE | re.DOTALL)
    description = desc_match.group(1).strip() if desc_match else ""

    # Try to extract frontmatter metadata
    metadata = {}
    if content.startswith("---"):
        try:
            parts = content.split("---", 2)
            if len(parts) >= 2:
                import yaml
                frontmatter = yaml.safe_load(parts[1])
                if isinstance(frontmatter, dict):
                    metadata = frontmatter.get("metadata", {})
        except ImportError:
            pass
        except Exception:
            pass

    return {
        "name": skill_dir.name,
        "title": title,
        "description": description[:100] + "..." if len(description) > 100 else description,
        "path": str(skill_dir),
        "metadata": metadata,
    }


def load_disabled_skills() -> List[str]:
    """Load the list of disabled skill names"""
    if not SKILLS_STATE_FILE.exists():
        return []
    try:
        with open(SKILLS_STATE_FILE) as f:
            state = json.load(f)
            return state.get("disabled", [])
    except (json.JSONDecodeError, OSError):
        return []


def list_installed_skills(skills_dirs: List[Path]) -> List[Dict]:
    """List all installed skills"""
    skills = []
    seen = set()
    disabled = load_disabled_skills()

    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue

        for item in skills_dir.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue

            # Skip duplicates (higher priority directories come first)
            if item.name in seen:
                continue
            seen.add(item.name)

            # Check if it has SKILL.md
            skill_md = item / "SKILL.md"
            if not skill_md.exists():
                continue

            skill_info = parse_skill_metadata(item)
            skill_info["source"] = str(skills_dir)
            skill_info["enabled"] = item.name not in disabled
            skills.append(skill_info)

    return skills


def main():
    parser = argparse.ArgumentParser(description="List skills")
    parser.add_argument("--filter", choices=["all", "installed", "available"],
                       default="all", help="Filter skills")
    parser.add_argument("--json", action="store_true", help="Output JSON format")

    args = parser.parse_args()

    skills_dirs = find_skills_directories()

    if not skills_dirs:
        print("No skills directories found", file=sys.stderr)
        sys.exit(1)

    installed_skills = list_installed_skills(skills_dirs)

    if args.json:
        print(json.dumps({
            "skills_directories": [str(d) for d in skills_dirs],
            "skills": installed_skills
        }, indent=2))
    else:
        print(f"Skills directories:")
        for d in skills_dirs:
            print(f"  - {d}")
        print()

        if not installed_skills:
            print("No skills installed")
        else:
            print(f"Installed skills ({len(installed_skills)}):\n")
            for skill in installed_skills:
                status = "enabled" if skill.get("enabled", True) else "disabled"
                icon = "📦" if skill.get("enabled", True) else "⏸️"
                print(f"{icon} {skill['title']} ({skill['name']}) [{status}]")
                print(f"   {skill['description']}")
                print(f"   Path: {skill['path']}")
                print()


if __name__ == "__main__":
    main()
