"""Telegram 文件/图片发送辅助。

#38 起 dolphin 已移除:本类不再是 dolphin Skillkit(原 ``_tg_send_file``/``_tg_send_photo``
工具 + ``_createSkills`` 注册已删)。milkie 下 telegram 文件发送走 alfred channel 的
输出约定(``<<<send_file: ...>>>``,见 :mod:`attachment_directives`),由 channel 调用
本类的 ``_send_document``/``_send_photo_api``/``_validate_file`` 完成实际投递。

保持原方法名以最小化 channel 改动。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from ..core.models.constants import TIMEOUT_UPLOAD, LIMIT_CAPTION

logger = logging.getLogger(__name__)

# Telegram Bot API 文件大小限制 (50 MB)
_TG_FILE_SIZE_LIMIT = 50 * 1024 * 1024

# 图片扩展名
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


class TelegramSkillkit:
    """Telegram 文件/图片发送辅助(纯 HTTP,无 dolphin 依赖)。"""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    async def _send_document(self, chat_id: str, file_path: str, caption: str = "") -> dict:
        """调用 Telegram sendDocument API。"""
        filename = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=TIMEOUT_UPLOAD) as client:
            with open(file_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:LIMIT_CAPTION]
                resp = await client.post(
                    f"{self._base_url}/sendDocument",
                    data=data,
                    files={"document": (filename, f)},
                )
                return resp.json()

    async def _send_photo_api(self, chat_id: str, file_path: str, caption: str = "") -> dict:
        """调用 Telegram sendPhoto API。"""
        filename = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=TIMEOUT_UPLOAD) as client:
            with open(file_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:LIMIT_CAPTION]
                resp = await client.post(
                    f"{self._base_url}/sendPhoto",
                    data=data,
                    files={"photo": (filename, f)},
                )
                return resp.json()

    def _validate_file(self, file_path: str) -> str:
        """校验文件,返回规范化路径。"""
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

    @staticmethod
    def is_image(file_path: str) -> bool:
        return Path(file_path).suffix.lower() in _IMAGE_EXTENSIONS
