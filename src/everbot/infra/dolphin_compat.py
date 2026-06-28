"""会话历史 context 变量名常量(dolphin-free)。

#38 起 dolphin 已彻底移除。``KEY_HISTORY*`` 是 milkie/alfred 会话历史用的纯字符串常量
(原与 dolphin 一致,主干 + 测试均依赖此具体值);core_service / session /
skill_change_detector / context_manager 仍 import 它。
"""

# 会话历史 context 变量名(沿用原 dolphin 常量值,主干 + 测试均依赖此具体值)。
KEY_HISTORY: str = "_history"
KEY_HISTORY_COMPACT_ON_PERSIST: str = "_history_compact_on_persist"
KEY_HISTORY_COMPACT_RECENT_TURNS: str = "_history_compact_recent_turns"
