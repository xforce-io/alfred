"""Compatibility shim for Dolphin runtime behaviors.

The canonical home for these helpers is now
``everbot.core.agent.provider.dolphin.compat``.  This module re-exports the
constants from there so existing import paths keep working.

``flags`` and ``ensure_continue_chat_compatibility`` stay defined here because
``tests/unit/test_dolphin_compat.py`` patches
``src.everbot.infra.dolphin_compat.flags``.
"""

from dolphin.core import flags

from ..core.agent.provider.dolphin.compat import (
    KEY_HISTORY,
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)

__all__ = [
    "flags",
    "KEY_HISTORY",
    "KEY_HISTORY_COMPACT_ON_PERSIST",
    "KEY_HISTORY_COMPACT_RECENT_TURNS",
    "ensure_continue_chat_compatibility",
]


def ensure_continue_chat_compatibility() -> bool:
    """
    Ensure runtime flags are compatible with ``continue_chat``.

    Returns:
        True if a flag value was changed, otherwise False.
    """
    if flags.is_enabled(flags.EXPLORE_BLOCK_V2):
        flags.set_flag(flags.EXPLORE_BLOCK_V2, False)
        return True
    return False
