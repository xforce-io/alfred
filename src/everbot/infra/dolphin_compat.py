"""
Compatibility helpers for Dolphin runtime behaviors.

主干多处 eager import 本模块,故它**不得**在导入期硬依赖 dolphin —— 否则
milkie-only 部署(未装 dolphin)连主干都 import 不了。dolphin 在时行为字节级不变;
缺失时 ``flags`` 退化为 None,``ensure_continue_chat_compatibility`` 变 no-op
(该 flag 调整本就是 dolphin runtime 专属,milkie 无对应)。(#38 去硬依赖)
"""

try:
    from dolphin.core import flags
except ImportError:  # milkie-only 部署未装 dolphin
    flags = None  # type: ignore[assignment]

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
    if flags is None:  # dolphin 未安装(milkie-only)→ 无此 flag,no-op
        return False
    if flags.is_enabled(flags.EXPLORE_BLOCK_V2):
        flags.set_flag(flags.EXPLORE_BLOCK_V2, False)
        return True
    return False
