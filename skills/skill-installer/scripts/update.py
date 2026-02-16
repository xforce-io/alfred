#!/usr/bin/env python3
"""
Update installed skills to the latest version
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Optional, List


def load_registry() -> Dict:
    """Load skill registry from local or remote"""
    # Try local registry first
    local_registry = Path.home() / ".alfred" / "skills-registry.json"
    if local_registry.exists():
        with open(local_registry) as f:
            return json.load(f)

    # Try remote registry
    registry_url = os.environ.get(
        "ALFRED_SKILL_REGISTRY",
        "https://raw.githubusercontent.com/your-org/alfred-skills/main/registry.json"
    )

    try:
        with urllib.request.urlopen(registry_url, timeout=10) as response:
            return json.loads(response.read())
    except Exception as e:
        print(f"Error loading registry: {e}", file=sys.stderr)
        return {"skills": {}}


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


def find_installed_skill(skill_name: str) -> Optional[Path]:
    """Find the installed skill directory"""
    skills_dirs = find_skills_directories()

    for skills_dir in skills_dirs:
        skill_path = skills_dir / skill_name
        if skill_path.exists() and (skill_path / "SKILL.md").exists():
            return skill_path

    return None


def get_skill_source_info(skill_path: Path) -> Optional[Dict]:
    """
    Try to determine the source of an installed skill
    Returns dict with 'type' and 'location' if found
    """
    # Check for .skill-source metadata file
    source_file = skill_path / ".skill-source"
    if source_file.exists():
        try:
            with open(source_file) as f:
                return json.load(f)
        except Exception:
            pass

    # Check if it's a git repository
    git_dir = skill_path / ".git"
    if git_dir.exists():
        try:
            # Get remote URL
            result = subprocess.run(
                ["git", "-C", str(skill_path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True
            )
            return {
                "type": "git",
                "location": result.stdout.strip()
            }
        except subprocess.CalledProcessError:
            pass

    return None


def save_skill_source_info(skill_path: Path, source_info: Dict):
    """Save source information for future updates"""
    source_file = skill_path / ".skill-source"
    with open(source_file, "w") as f:
        json.dump(source_info, f, indent=2)


def update_skill_from_git(skill_path: Path, url: Optional[str] = None) -> bool:
    """Update skill from git repository"""
    git_dir = skill_path / ".git"

    if git_dir.exists():
        # Pull latest changes
        print(f"Pulling latest changes for {skill_path.name}...")
        try:
            subprocess.run(
                ["git", "-C", str(skill_path), "pull"],
                check=True,
                capture_output=True,
                text=True
            )
            print("✓ Updated successfully")
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error pulling updates: {e.stderr}", file=sys.stderr)
            return False
    elif url:
        # Re-clone from URL
        print(f"Re-installing {skill_path.name} from {url}...")
        # Backup current version
        backup_path = skill_path.parent / f"{skill_path.name}.backup"
        if backup_path.exists():
            shutil.rmtree(backup_path)
        shutil.move(str(skill_path), str(backup_path))

        try:
            subprocess.run(
                ["git", "clone", url, str(skill_path)],
                check=True,
                capture_output=True,
                text=True
            )
            # Remove backup
            shutil.rmtree(backup_path)
            # Remove .git to avoid confusion
            git_dir = skill_path / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)
            print("✓ Updated successfully")
            return True
        except subprocess.CalledProcessError as e:
            # Restore backup
            if backup_path.exists():
                shutil.rmtree(skill_path, ignore_errors=True)
                shutil.move(str(backup_path), str(skill_path))
            print(f"Error updating: {e.stderr}", file=sys.stderr)
            print("Restored previous version")
            return False
    else:
        print(f"Cannot update: no git repository and no source URL", file=sys.stderr)
        return False


def update_skill_from_registry(skill_name: str, registry: Dict) -> bool:
    """Update skill from registry"""
    if skill_name not in registry.get("skills", {}):
        print(f"Error: Skill '{skill_name}' not found in registry", file=sys.stderr)
        return False

    skill_info = registry["skills"][skill_name]
    source_info = skill_info.get("source", {})
    source_type = source_info.get("type")
    location = source_info.get("location")

    if not location:
        print(f"Error: No source location in registry for '{skill_name}'", file=sys.stderr)
        return False

    skill_path = find_installed_skill(skill_name)
    if not skill_path:
        print(f"Error: Skill '{skill_name}' is not installed", file=sys.stderr)
        return False

    if source_type == "git":
        return update_skill_from_git(skill_path, location)
    else:
        print(f"Update not supported for source type: {source_type}", file=sys.stderr)
        print("Try removing and reinstalling the skill instead.")
        return False


def update_skill(skill_name: str, registry: Optional[Dict] = None) -> bool:
    """
    Update a skill to the latest version

    Args:
        skill_name: Name of the skill to update
        registry: Optional registry dict (will load if not provided)

    Returns:
        True if successful
    """
    skill_path = find_installed_skill(skill_name)

    if not skill_path:
        print(f"Error: Skill '{skill_name}' is not installed", file=sys.stderr)
        return False

    print(f"Updating skill: {skill_name}")
    print(f"Location: {skill_path}")

    # Try to get source info
    source_info = get_skill_source_info(skill_path)

    if source_info:
        # We know the source
        print(f"Source: {source_info['type']} - {source_info['location']}")

        if source_info['type'] == 'git':
            return update_skill_from_git(skill_path, source_info['location'])
        else:
            print(f"Update not supported for source type: {source_info['type']}", file=sys.stderr)
            return False
    else:
        # Try to find in registry
        print("Checking registry for update source...")
        if registry is None:
            registry = load_registry()

        return update_skill_from_registry(skill_name, registry)


def update_all_skills() -> bool:
    """Update all installed skills"""
    skills_dirs = find_skills_directories()
    registry = load_registry()

    updated_count = 0
    failed_count = 0
    skipped_count = 0

    all_skills = []
    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue

        for item in skills_dir.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue

            skill_md = item / "SKILL.md"
            if skill_md.exists():
                all_skills.append((item.name, item))

    if not all_skills:
        print("No installed skills found")
        return True

    print(f"Found {len(all_skills)} skill(s) to check for updates\n")

    for skill_name, skill_path in all_skills:
        print(f"\n{'='*60}")
        print(f"Checking: {skill_name}")
        print(f"{'='*60}")

        # Skip skill-installer itself
        if skill_name == "skill-installer":
            print("Skipping skill-installer (meta-skill)")
            skipped_count += 1
            continue

        try:
            if update_skill(skill_name, registry):
                updated_count += 1
            else:
                failed_count += 1
        except Exception as e:
            print(f"Error updating {skill_name}: {e}", file=sys.stderr)
            failed_count += 1

    print(f"\n{'='*60}")
    print("Update Summary:")
    print(f"  Updated: {updated_count}")
    print(f"  Failed: {failed_count}")
    print(f"  Skipped: {skipped_count}")
    print(f"{'='*60}")

    return failed_count == 0


def main():
    parser = argparse.ArgumentParser(description="Update installed skills")
    parser.add_argument("skill_name", nargs="?", help="Skill name to update (omit for all)")
    parser.add_argument("--all", action="store_true", help="Update all installed skills")

    args = parser.parse_args()

    if args.all or args.skill_name is None:
        success = update_all_skills()
    else:
        success = update_skill(args.skill_name)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
