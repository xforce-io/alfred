#!/usr/bin/env python3
"""
Remove installed skills
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional


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


def remove_skill(skill_name: str, force: bool = False, keep_backup: bool = False) -> bool:
    """
    Remove an installed skill

    Args:
        skill_name: Name of the skill to remove
        force: Skip confirmation prompt
        keep_backup: Keep a backup of the removed skill

    Returns:
        True if successful
    """
    skill_path = find_installed_skill(skill_name)

    if not skill_path:
        print(f"Error: Skill '{skill_name}' is not installed", file=sys.stderr)
        return False

    # Protected skills that should not be removed
    protected_skills = ["skill-installer"]
    if skill_name in protected_skills:
        print(f"Error: Cannot remove protected skill '{skill_name}'", file=sys.stderr)
        print("This skill is essential for skill management.")
        return False

    print(f"Skill to remove: {skill_name}")
    print(f"Location: {skill_path}")

    # Confirmation prompt
    if not force:
        response = input(f"\nAre you sure you want to remove '{skill_name}'? [y/N] ")
        if response.lower() != "y":
            print("Cancelled")
            return False

    try:
        if keep_backup:
            # Create backup
            backup_path = skill_path.parent / f"{skill_name}.backup.{int(__import__('time').time())}"
            print(f"Creating backup at: {backup_path}")
            shutil.copytree(skill_path, backup_path)

        # Remove the skill
        print(f"Removing {skill_name}...")
        shutil.rmtree(skill_path)

        print(f"✓ Skill '{skill_name}' removed successfully")

        if keep_backup:
            print(f"Backup saved at: {backup_path}")
            print("You can restore it by moving it back to the skills directory")

        return True

    except PermissionError:
        print(f"Error: Permission denied. Try running with sudo or check file permissions.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error removing skill: {e}", file=sys.stderr)
        return False


def list_removable_skills() -> List[str]:
    """List all installed skills that can be removed"""
    skills_dirs = find_skills_directories()
    protected_skills = {"skill-installer"}
    removable = []

    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue

        for item in skills_dir.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue

            skill_md = item / "SKILL.md"
            if skill_md.exists() and item.name not in protected_skills:
                removable.append(item.name)

    return sorted(set(removable))


def remove_multiple_skills(skill_names: List[str], force: bool = False, keep_backup: bool = False) -> bool:
    """Remove multiple skills"""
    if not skill_names:
        print("No skills specified", file=sys.stderr)
        return False

    print(f"Skills to remove: {', '.join(skill_names)}")

    if not force:
        response = input(f"\nRemove {len(skill_names)} skill(s)? [y/N] ")
        if response.lower() != "y":
            print("Cancelled")
            return False

    success_count = 0
    failed_count = 0

    for skill_name in skill_names:
        print(f"\n{'='*60}")
        try:
            # Use force=True since we already confirmed above
            if remove_skill(skill_name, force=True, keep_backup=keep_backup):
                success_count += 1
            else:
                failed_count += 1
        except Exception as e:
            print(f"Error removing {skill_name}: {e}", file=sys.stderr)
            failed_count += 1

    print(f"\n{'='*60}")
    print("Remove Summary:")
    print(f"  Removed: {success_count}")
    print(f"  Failed: {failed_count}")
    print(f"{'='*60}")

    return failed_count == 0


def interactive_remove():
    """Interactive mode to select skills to remove"""
    removable_skills = list_removable_skills()

    if not removable_skills:
        print("No removable skills found")
        return False

    print("Removable skills:\n")
    for i, skill_name in enumerate(removable_skills, 1):
        skill_path = find_installed_skill(skill_name)
        print(f"{i:2d}. {skill_name}")
        if skill_path:
            print(f"    {skill_path}")

    print("\nEnter skill numbers to remove (comma-separated), or 'all' for all:")
    print("(Press Enter to cancel)")

    try:
        selection = input("> ").strip()

        if not selection:
            print("Cancelled")
            return False

        if selection.lower() == "all":
            return remove_multiple_skills(removable_skills)

        # Parse selection
        indices = [int(x.strip()) - 1 for x in selection.split(",")]
        selected_skills = [removable_skills[i] for i in indices if 0 <= i < len(removable_skills)]

        if not selected_skills:
            print("No valid skills selected")
            return False

        return remove_multiple_skills(selected_skills)

    except (ValueError, IndexError) as e:
        print(f"Invalid selection: {e}", file=sys.stderr)
        return False
    except KeyboardInterrupt:
        print("\nCancelled")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Remove installed skills",
        epilog="Examples:\n"
               "  remove.py my-skill              # Remove a specific skill\n"
               "  remove.py skill1 skill2         # Remove multiple skills\n"
               "  remove.py -i                    # Interactive mode\n"
               "  remove.py my-skill -f           # Force remove without confirmation\n"
               "  remove.py my-skill --backup     # Remove but keep backup\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("skill_names", nargs="*", help="Skill name(s) to remove")
    parser.add_argument("-f", "--force", action="store_true",
                       help="Skip confirmation prompt")
    parser.add_argument("-i", "--interactive", action="store_true",
                       help="Interactive mode to select skills")
    parser.add_argument("--backup", action="store_true",
                       help="Keep a backup of removed skill(s)")
    parser.add_argument("--list", action="store_true",
                       help="List removable skills and exit")

    args = parser.parse_args()

    # List mode
    if args.list:
        removable = list_removable_skills()
        if removable:
            print("Removable skills:")
            for skill in removable:
                skill_path = find_installed_skill(skill)
                print(f"  - {skill}")
                if skill_path:
                    print(f"    {skill_path}")
        else:
            print("No removable skills found")
        return

    # Interactive mode
    if args.interactive:
        success = interactive_remove()
        sys.exit(0 if success else 1)

    # Regular mode
    if not args.skill_names:
        parser.print_help()
        sys.exit(1)

    if len(args.skill_names) == 1:
        success = remove_skill(args.skill_names[0], args.force, args.backup)
    else:
        success = remove_multiple_skills(args.skill_names, args.force, args.backup)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
