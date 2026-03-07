#!/usr/bin/env bash
# dev-setup.sh — Symlink all repo skills into ~/.alfred/skills/ for development.
# Run once after cloning. Changes to skills/ take effect immediately without reinstall.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS_SRC="$REPO_ROOT/skills"
INSTALLER="$SKILLS_SRC/skill-installer/scripts/install.py"

echo "Setting up dev skill symlinks: $SKILLS_SRC -> ~/.alfred/skills/"
echo ""

for skill_dir in "$SKILLS_SRC"/*/; do
    skill_name="$(basename "$skill_dir")"
    target="$HOME/.alfred/skills/$skill_name"

    if [ -L "$target" ]; then
        echo "  [skip] $skill_name (already symlinked)"
        continue
    fi

    python3 "$INSTALLER" "$skill_dir" --dev <<< "y"
    echo "  [ok]   $skill_name"
done

echo ""
echo "Done. All skills are symlinked. Edit skills/ directly — no reinstall needed."
