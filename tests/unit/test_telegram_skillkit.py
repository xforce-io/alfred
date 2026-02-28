"""Unit tests for TelegramSkillkit."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.channels.telegram_skillkit import (
    TelegramSkillkit,
    _resolve_chat_id,
    _TG_FILE_SIZE_LIMIT,
)


# ===========================================================================
# _resolve_chat_id
# ===========================================================================


class TestResolveChatId:
    def test_no_props_raises(self):
        with pytest.raises(RuntimeError, match="props is None"):
            _resolve_chat_id(None)

    def test_no_gvp_raises(self):
        with pytest.raises(RuntimeError, match="缺少 gvp"):
            _resolve_chat_id({"some_key": "value"})

    def test_no_session_id_raises(self):
        gvp = MagicMock()
        gvp.get_session_id.return_value = None
        gvp.get_var_value.return_value = None
        with pytest.raises(RuntimeError, match="无法获取 session_id"):
            _resolve_chat_id({"gvp": gvp})

    def test_extract_fails_raises(self):
        gvp = MagicMock()
        gvp.get_session_id.return_value = "bad_session_id"
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = ""
            with pytest.raises(RuntimeError, match="无法从 session_id"):
                _resolve_chat_id({"gvp": gvp})

    def test_success_via_get_session_id(self):
        gvp = MagicMock()
        gvp.get_session_id.return_value = "tg_session_agent__12345"
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "12345"
            assert _resolve_chat_id({"gvp": gvp}) == "12345"

    def test_fallback_to_get_var_value(self):
        gvp = MagicMock(spec=[])  # no get_session_id
        gvp.get_var_value = MagicMock(return_value="tg_session_agent__999")
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "999"
            assert _resolve_chat_id({"gvp": gvp}) == "999"


# ===========================================================================
# _validate_file
# ===========================================================================


class TestValidateFile:
    @pytest.fixture
    def skillkit(self):
        return TelegramSkillkit(bot_token="123:FAKE")

    def test_valid_file(self, skillkit, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = skillkit._validate_file(str(f))
        assert os.path.isfile(result)

    def test_file_not_found(self, skillkit):
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            skillkit._validate_file("/nonexistent/file.txt")

    def test_file_too_large(self, skillkit, tmp_path):
        f = tmp_path / "big.bin"
        # Create a file that reports as too large via mock
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
        result = skillkit._validate_file(f"'{f}'")
        assert result == os.path.normpath(str(f))

    def test_expands_user(self, skillkit, tmp_path):
        f = tmp_path / "user.txt"
        f.write_text("data")
        with patch("os.path.expanduser", return_value=str(f)):
            result = skillkit._validate_file("~/user.txt")
            assert os.path.isfile(result)


# ===========================================================================
# _tg_send_file
# ===========================================================================


class TestTgSendFile:
    @pytest.fixture
    def skillkit(self):
        return TelegramSkillkit(bot_token="123:FAKE")

    @pytest.fixture
    def props(self):
        gvp = MagicMock()
        gvp.get_session_id.return_value = "tg_session_agent__111"
        return {"gvp": gvp}

    @pytest.mark.asyncio
    async def test_send_file_success(self, skillkit, tmp_path, props):
        f = tmp_path / "report.csv"
        f.write_text("a,b,c")
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "111"
            skillkit._send_document = AsyncMock(return_value={"ok": True})
            result = await skillkit._tg_send_file(str(f), caption="data", props=props)
        assert "成功" in result

    @pytest.mark.asyncio
    async def test_send_file_failure(self, skillkit, tmp_path, props):
        f = tmp_path / "report.csv"
        f.write_text("a,b,c")
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "111"
            skillkit._send_document = AsyncMock(
                return_value={"ok": False, "description": "Bad Request"}
            )
            result = await skillkit._tg_send_file(str(f), props=props)
        assert "失败" in result
        assert "Bad Request" in result


# ===========================================================================
# _tg_send_photo
# ===========================================================================


class TestTgSendPhoto:
    @pytest.fixture
    def skillkit(self):
        return TelegramSkillkit(bot_token="123:FAKE")

    @pytest.fixture
    def props(self):
        gvp = MagicMock()
        gvp.get_session_id.return_value = "tg_session_agent__111"
        return {"gvp": gvp}

    @pytest.mark.asyncio
    async def test_send_photo_success(self, skillkit, tmp_path, props):
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0fake")
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "111"
            skillkit._send_photo_api = AsyncMock(return_value={"ok": True})
            result = await skillkit._tg_send_photo(str(f), props=props)
        assert "成功" in result
        skillkit._send_photo_api.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_image_falls_back_to_document(self, skillkit, tmp_path, props):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "111"
            skillkit._send_document = AsyncMock(return_value={"ok": True})
            skillkit._send_photo_api = AsyncMock()
            result = await skillkit._tg_send_photo(str(f), props=props)
        assert "成功" in result
        skillkit._send_document.assert_awaited_once()
        skillkit._send_photo_api.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_photo_api_fail_falls_back_to_document(self, skillkit, tmp_path, props):
        f = tmp_path / "big.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 100)
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "111"
            skillkit._send_photo_api = AsyncMock(
                return_value={"ok": False, "description": "photo too large"}
            )
            skillkit._send_document = AsyncMock(return_value={"ok": True})
            result = await skillkit._tg_send_photo(str(f), props=props)
        assert "成功" in result
        skillkit._send_photo_api.assert_awaited_once()
        skillkit._send_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_fail(self, skillkit, tmp_path, props):
        f = tmp_path / "bad.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 10)
        with patch(
            "src.everbot.channels.telegram_skillkit.ChannelSessionResolver"
        ) as mock_cls:
            mock_cls.extract_channel_session_id.return_value = "111"
            skillkit._send_photo_api = AsyncMock(return_value={"ok": False})
            skillkit._send_document = AsyncMock(
                return_value={"ok": False, "description": "server error"}
            )
            result = await skillkit._tg_send_photo(str(f), props=props)
        assert "失败" in result


# ===========================================================================
# _createSkills
# ===========================================================================


class TestCreateSkills:
    def test_creates_two_skills(self):
        sk = TelegramSkillkit(bot_token="123:FAKE")
        skills = sk._createSkills()
        assert len(skills) == 2
        names = {s.get_function_name() for s in skills}
        assert "_tg_send_file" in names
        assert "_tg_send_photo" in names
