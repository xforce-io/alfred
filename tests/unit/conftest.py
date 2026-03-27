"""Unit test configuration — patches missing dolphin constants for compatibility.

The installed dolphin version may not have all constants used by the src code.
This conftest shims them in at import time so that test collection succeeds.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path (redundant with top-level conftest, but
# explicit is better than implicit in case this conftest is loaded first).
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _patch_dolphin_constants() -> None:
    """Shim constants missing from the installed dolphin version.

    The installed dolphin package may be older than what the src code requires.
    These shims ensure test collection and execution succeed without needing to
    upgrade the environment.
    """
    import dolphin.core.common.constants as _dc

    _MISSING: dict = {
        "KEY_HISTORY": "history_messages",
        "KEY_HISTORY_COMPACT_ON_PERSIST": "history_compact_on_persist",
        "KEY_HISTORY_COMPACT_RECENT_TURNS": "history_compact_recent_turns",
    }
    for name, default in _MISSING.items():
        if not hasattr(_dc, name):
            setattr(_dc, name, default)


_patch_dolphin_constants()
