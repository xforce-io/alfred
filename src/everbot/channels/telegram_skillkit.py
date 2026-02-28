"""Telegram Channel Skillkit — 让智能体能通过 Telegram 发送文件和图片。

注册到 Dolphin Agent 后，智能体可以调用 _tg_send_file / _tg_send_photo
直接向当前 Telegram 对话发送文件。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

import httpx

from dolphin.core.skill.skillkit import Skillkit
from dolphin.core.skill.skill_function import SkillFunction

from ..core.channel.session_resolver import ChannelSessionResolver

logger = logging.getLogger(__name__)

# Telegram Bot API 文件大小限制 (50 MB)
_TG_FILE_SIZE_LIMIT = 50 * 1024 * 1024

# 图片扩展名（用于 _tg_send_photo 自动判断）
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def _resolve_chat_id(props: dict | None) -> str:
    """从 Dolphin 运行时 props 中提取 Telegram chat_id。"""
    if not props:
        raise RuntimeError("无法获取运行时上下文 (props is None)")
    context = props.get("gvp")
    if context is None:
        raise RuntimeError("无法获取运行时上下文 (props 中缺少 gvp)")

    session_id = None
    if hasattr(context, "get_session_id"):
        session_id = context.get_session_id()
    if not session_id and hasattr(context, "get_var_value"):
        session_id = context.get_var_value("session_id")
    if not session_id:
        raise RuntimeError("无法获取 session_id，请确保在 Telegram 渠道中使用此工具")

    chat_id = ChannelSessionResolver.extract_channel_session_id(session_id)
    if not chat_id:
        raise RuntimeError(
            f"无法从 session_id '{session_id}' 中提取 chat_id，"
            "此工具仅支持在 Telegram 渠道中使用"
        )
    return chat_id


class TelegramSkillkit(Skillkit):
    """Dolphin Skillkit providing Telegram file/photo sending capabilities."""

    def __init__(self, bot_token: str) -> None:
        super().__init__()
        self._bot_token = bot_token
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    def getName(self) -> str:
        return "telegram_channel"

    async def _send_document(self, chat_id: str, file_path: str, caption: str = "") -> dict:
        """调用 Telegram sendDocument API。"""
        filename = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=120) as client:
            with open(file_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:1024]  # Telegram caption 限制
                resp = await client.post(
                    f"{self._base_url}/sendDocument",
                    data=data,
                    files={"document": (filename, f)},
                )
                return resp.json()

    async def _send_photo_api(self, chat_id: str, file_path: str, caption: str = "") -> dict:
        """调用 Telegram sendPhoto API。"""
        filename = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=120) as client:
            with open(file_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                resp = await client.post(
                    f"{self._base_url}/sendPhoto",
                    data=data,
                    files={"photo": (filename, f)},
                )
                return resp.json()

    def _validate_file(self, file_path: str) -> str:
        """校验文件，返回规范化路径。"""
        file_path = file_path.strip().strip("'\"`")
        file_path = os.path.expandvars(file_path)
        file_path = os.path.expanduser(file_path)
        file_path = os.path.normpath(file_path)

        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)
        if file_size > _TG_FILE_SIZE_LIMIT:
            size_mb = file_size / (1024 * 1024)
            raise ValueError(f"文件过大 ({size_mb:.1f} MB)，Telegram 限制 50 MB")

        if file_size == 0:
            raise ValueError("文件为空")

        return file_path

    async def _tg_send_file(self, file_path: str, caption: str = "", **kwargs) -> str:
        """发送本地文件给当前 Telegram 用户。

        Args:
            file_path (str): 本地文件的绝对路径
            caption (str): 可选的文件说明文字

        Returns:
            str: 发送结果
        """
        props = kwargs.get("props")
        chat_id = _resolve_chat_id(props)
        file_path = self._validate_file(file_path)
        filename = os.path.basename(file_path)

        result = await self._send_document(chat_id, file_path, caption)
        if result.get("ok"):
            return f"文件 '{filename}' 已成功发送"
        else:
            desc = result.get("description", "未知错误")
            return f"文件发送失败: {desc}"

    async def _tg_send_photo(self, file_path: str, caption: str = "", **kwargs) -> str:
        """发送本地图片给当前 Telegram 用户（带缩略图预览）。

        Args:
            file_path (str): 本地图片的绝对路径（支持 jpg/png/gif/webp）
            caption (str): 可选的图片说明文字

        Returns:
            str: 发送结果
        """
        props = kwargs.get("props")
        chat_id = _resolve_chat_id(props)
        file_path = self._validate_file(file_path)
        filename = os.path.basename(file_path)

        # 检查是否为支持的图片格式
        ext = Path(file_path).suffix.lower()
        if ext not in _IMAGE_EXTENSIONS:
            # 非图片格式，降级为 sendDocument
            result = await self._send_document(chat_id, file_path, caption)
        else:
            result = await self._send_photo_api(chat_id, file_path, caption)
            # sendPhoto 对大图可能失败，降级为 sendDocument
            if not result.get("ok"):
                result = await self._send_document(chat_id, file_path, caption)

        if result.get("ok"):
            return f"图片 '{filename}' 已成功发送"
        else:
            desc = result.get("description", "未知错误")
            return f"图片发送失败: {desc}"

    def _createSkills(self) -> List[SkillFunction]:
        return [
            SkillFunction(self._tg_send_file),
            SkillFunction(self._tg_send_photo),
        ]
