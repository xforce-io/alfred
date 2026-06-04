"""TelegramSkillkit 单测(#38 后:纯发送辅助,无 dolphin)。

原 _resolve_chat_id / _tg_send_file / _tg_send_photo / getSkills(dolphin 工具方法)已删;
发送编排(含 photo→document 降级)现由 channel 的 _send_attachment_directives 覆盖
(见 test_telegram_attachment_send)。本文件只测保留的文件校验 + 图片判定。
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.everbot.channels.telegram_skillkit import TelegramSkillkit, _TG_FILE_SIZE_LIMIT


class TestValidateFile:
    @pytest.fixture
    def skillkit(self):
        return TelegramSkillkit(bot_token="123:FAKE")

    def test_valid_file(self, skillkit, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert os.path.isfile(skillkit._validate_file(str(f)))

    def test_file_not_found(self, skillkit):
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            skillkit._validate_file("/nonexistent/file.txt")

    def test_file_too_large(self, skillkit, tmp_path):
        f = tmp_path / "big.bin"
        f.write_text("x")
        with patch("os.path.getsize", return_value=_TG_FILE_SIZE_LIMIT + 1):
            with pytest.raises(ValueError, match="文件过大"):
                skillkit._validate_file(str(f))

    def test_empty_file(self, skillkit, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        with pytest.raises(ValueError, match="文件为空"):
            skillkit._validate_file(str(f))

    def test_strips_quotes(self, skillkit, tmp_path):
        f = tmp_path / "quoted.txt"
        f.write_text("data")
        assert skillkit._validate_file(f"'{f}'") == os.path.normpath(str(f))


class TestIsImage:
    def test_image_extensions(self):
        assert TelegramSkillkit.is_image("/a/b.png")
        assert TelegramSkillkit.is_image("/a/b.JPG")
        assert not TelegramSkillkit.is_image("/a/b.csv")
