"""
Compatibility helpers for Dolphin runtime behaviors.
"""

from dolphin.core import flags

# KEY_HISTORY was removed in kweaver-dolphin 0.2.4; the underlying variable
# name is the plain string "history".
try:
    from dolphin.core.common.constants import KEY_HISTORY  # noqa: F401
except ImportError:
    KEY_HISTORY: str = "history"  # type: ignore[no-redef]

# KEY_HISTORY_COMPACT_* may also be removed in future versions.
try:
    from dolphin.core.common.constants import KEY_HISTORY_COMPACT_ON_PERSIST  # noqa: F401
except ImportError:
    KEY_HISTORY_COMPACT_ON_PERSIST: str = "_history_compact_on_persist"  # type: ignore[no-redef]

try:
    from dolphin.core.common.constants import KEY_HISTORY_COMPACT_RECENT_TURNS  # noqa: F401
except ImportError:
    KEY_HISTORY_COMPACT_RECENT_TURNS: str = "_history_compact_recent_turns"  # type: ignore[no-redef]


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
