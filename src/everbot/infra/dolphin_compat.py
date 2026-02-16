"""
Compatibility helpers for Dolphin runtime behaviors.
"""

from dolphin.core import flags


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
