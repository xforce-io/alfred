#!/usr/bin/env python3
"""
Search skills in the registry
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List


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


def search_skills(query: str, registry: Dict) -> List[Dict]:
    """Search skills by query string"""
    query_lower = query.lower()
    results = []

    for skill_name, skill_info in registry.get("skills", {}).items():
        # Search in name, description, and tags
        searchable_text = f"{skill_name} {skill_info.get('description', '')} {' '.join(skill_info.get('tags', []))}"

        if query_lower in searchable_text.lower():
            results.append({
                "name": skill_name,
                "display_name": skill_info.get("name", skill_name),
                "description": skill_info.get("description", ""),
                "version": skill_info.get("version", "unknown"),
                "requires": skill_info.get("requires", {}),
            })

    return results


def main():
    parser = argparse.ArgumentParser(description="Search for skills")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--json", action="store_true", help="Output JSON format")

    args = parser.parse_args()

    registry = load_registry()
    results = search_skills(args.query, registry)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print(f"No skills found matching '{args.query}'")
            sys.exit(1)

        print(f"Found {len(results)} skill(s):\n")
        for skill in results:
            print(f"📦 {skill['display_name']} ({skill['name']}) - v{skill['version']}")
            print(f"   {skill['description']}")

            if skill['requires']:
                reqs = []
                if skill['requires'].get('bins'):
                    reqs.append(f"bins: {', '.join(skill['requires']['bins'])}")
                if skill['requires'].get('env'):
                    reqs.append(f"env: {', '.join(skill['requires']['env'])}")
                if reqs:
                    print(f"   Requires: {'; '.join(reqs)}")
            print()


if __name__ == "__main__":
    main()
