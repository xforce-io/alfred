#!/usr/bin/env python3
"""
Enable a disabled skill
"""

import argparse
import json
import sys
from pathlib import Path

SKILLS_STATE_FILE = Path.home() / ".alfred" / "skills-state.json"


def load_state() -> dict:
    """Load skills state file"""
    if not SKILLS_STATE_FILE.exists():
        return {"version": "1.0", "disabled": []}
    try:
        with open(SKILLS_STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": "1.0", "disabled": []}


def save_state(state: dict):
    """Save skills state file"""
    SKILLS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SKILLS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Enable a skill")
    parser.add_argument("skill_name", help="Name of the skill to enable")
    args = parser.parse_args()

    skill_name = args.skill_name
    state = load_state()
    disabled = state.get("disabled", [])

    if skill_name not in disabled:
        print(f"Skill '{skill_name}' is already enabled.")
        return 0

    disabled.remove(skill_name)
    state["disabled"] = disabled
    save_state(state)
    print(f"Skill '{skill_name}' has been enabled.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
