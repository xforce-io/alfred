"""Telegram 附件输出约定(#38 telegram 原生化)。

milkie 下 agent 无法 mid-turn 调 Python skillkit(ToolContext 不暴露 contextId →
拿不到 per-会话 chat_id),故文件/图片发送改为**输出约定**:agent 在回复文本里写
``<<<send_file: /abs/path | 可选说明>>>`` / ``<<<send_photo: ...>>>``;alfred channel
(它知道 chat_id)在 turn 结束后用现成 Telegram 发送辅助投递,并把标记从可见文本里剥掉。

dolphin agent 仍用 skillkit 工具、不会被注入该约定指令,故永不产出标记 —— 对 dolphin
零影响(标记解析只在出现标记时才动作)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

# <<<send_file: <path> [| caption]>>>  /  <<<send_photo: ...>>>
_DIRECTIVE_RE = re.compile(r"<<<send_(file|photo):\s*(.+?)\s*>>>", re.DOTALL)


@dataclass(frozen=True)
class AttachmentDirective:
    kind: str       # "file" | "photo"
    path: str
    caption: str = ""


def parse_attachment_directives(text: str) -> Tuple[str, List[AttachmentDirective]]:
    """从 text 提取附件标记,返回 (清理后的文本, 指令列表)。

    无标记时返回原文 + 空列表(零开销路径)。``path | caption`` 用首个 ``|`` 切分。
    清理后压缩多余空行。
    """
    if "<<<send_" not in text:
        return text, []

    directives: List[AttachmentDirective] = []

    def _collect(m: "re.Match[str]") -> str:
        kind = m.group(1)
        body = m.group(2).strip()
        if "|" in body:
            path, caption = body.split("|", 1)
            directives.append(AttachmentDirective(kind, path.strip(), caption.strip()))
        else:
            directives.append(AttachmentDirective(kind, body, ""))
        return ""

    cleaned = _DIRECTIVE_RE.sub(_collect, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, directives


# agent 提示里告诉 telegram-serving 的 milkie agent 如何请求发文件。
ATTACHMENT_INSTRUCTION = (
    "# 发送文件/图片给用户（Telegram）\n\n"
    "要把本地文件或图片发给用户，在你的回复里单独一行写：\n"
    "`<<<send_file: /绝对/路径 | 可选说明>>>`（普通文件）或 "
    "`<<<send_photo: /绝对/路径 | 可选说明>>>`（图片）。\n"
    "系统会自动投递该文件并把这行标记从用户可见文本中移除。路径必须是绝对路径。"
)
