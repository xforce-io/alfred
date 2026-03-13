"""Shared test configuration."""

import sys
from pathlib import Path

# Ensure project root is on sys.path so that `from src.everbot...` works
# regardless of how pytest is invoked (run_tests.sh, bare pytest, CI, etc.).
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
