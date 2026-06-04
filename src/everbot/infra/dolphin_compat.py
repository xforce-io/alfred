"""历史变量名常量 + continue_chat 兼容(dolphin-free)。

#38 起 dolphin 已彻底移除:本模块不再 import dolphin。``KEY_HISTORY*`` 是 milkie/alfred
会话历史用的纯字符串常量(原与 dolphin 一致);``ensure_continue_chat_compatibility``
是 dolphin runtime flag 专属调整,milkie 无对应 → 恒 no-op。保留模块名与符号以最小化
主干 import 改动(core_service / session / skill_change_detector / context_manager 仍 import 它)。
"""

# 会话历史 context 变量名(沿用原 dolphin 常量值,主干 + 测试均依赖此具体值)。
KEY_HISTORY: str = "_history"
KEY_HISTORY_COMPACT_ON_PERSIST: str = "_history_compact_on_persist"
KEY_HISTORY_COMPACT_RECENT_TURNS: str = "_history_compact_recent_turns"


def ensure_continue_chat_compatibility() -> bool:
    """dolphin runtime flag 兼容动作;dolphin 已移除 → no-op,恒返回 False。"""
    return False
