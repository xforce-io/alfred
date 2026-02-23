"""Unit tests for Telegram media download security hardening.

Covers:
- _sanitize_filename: path traversal, special chars, empty results
- _safe_local_path: symlink escape, .. traversal, normal paths
- Size limit enforcement in _download_document / _download_voice / _download_photo
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.channels.telegram_channel import TelegramChannel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel(tmp_path: Path) -> TelegramChannel:
    sm = MagicMock()
    sm.get_cached_agent.return_value = None
    sm.acquire_session = AsyncMock(return_value=True)
    sm.release_session = MagicMock()
    sm.persistence = MagicMock()
    sm.persistence._get_lock_path.return_value = tmp_path / "test_lock"
    ch = TelegramChannel(
        bot_token="123:FAKE",
        session_manager=sm,
        default_agent="test_agent",
    )
    ch._bindings_path = tmp_path / "bindings.json"
    return ch


# ===========================================================================
# _sanitize_filename
# ===========================================================================

class TestSanitizeFilename:
    def test_normal_filename(self):
        assert TelegramChannel._sanitize_filename("report.pdf") == "report.pdf"

    def test_strips_directory_components(self):
        assert TelegramChannel._sanitize_filename("../../etc/passwd") == "passwd"

    def test_strips_windows_path(self):
        # On Unix, backslashes are not path separators — Path().name keeps them,
        # then regex replaces them. The key guarantee: no ".." components survive.
        result = TelegramChannel._sanitize_filename("..\\..\\windows\\system32\\file.dll")
        assert ".." not in result
        assert result.endswith("file.dll")

    def test_replaces_special_chars(self):
        result = TelegramChannel._sanitize_filename("file name (1) [copy].txt")
        assert "/" not in result
        assert "\\" not in result
        assert ".txt" in result

    def test_strips_leading_dots(self):
        result = TelegramChannel._sanitize_filename("...hidden")
        assert not result.startswith(".")

    def test_empty_after_sanitize_returns_unnamed(self):
        assert TelegramChannel._sanitize_filename("/../../../") == "unnamed"

    def test_only_special_chars_returns_unnamed(self):
        assert TelegramChannel._sanitize_filename("...---___") == "unnamed"

    def test_preserves_extension(self):
        result = TelegramChannel._sanitize_filename("my document.docx")
        assert result.endswith(".docx")

    def test_unicode_chars_replaced(self):
        result = TelegramChannel._sanitize_filename("文件名.pdf")
        # Unicode chars are replaced with underscores; dot between name and ext
        # may get stripped if the sanitized base is only underscores + dot + "pdf"
        assert "pdf" in result
        assert "/" not in result
        assert ".." not in result


# ===========================================================================
# _safe_local_path
# ===========================================================================

class TestSafeLocalPath:
    def test_normal_path_allowed(self, tmp_path):
        ch = _make_channel(tmp_path)
        target_dir = tmp_path / "docs"
        target_dir.mkdir()
        result = ch._safe_local_path(target_dir, "report.pdf")
        assert result is not None
        assert str(result).startswith(str(target_dir.resolve()))

    def test_traversal_blocked(self, tmp_path):
        ch = _make_channel(tmp_path)
        target_dir = tmp_path / "docs"
        target_dir.mkdir()
        result = ch._safe_local_path(target_dir, "../escape.txt")
        assert result is None

    def test_double_traversal_blocked(self, tmp_path):
        ch = _make_channel(tmp_path)
        target_dir = tmp_path / "docs"
        target_dir.mkdir()
        result = ch._safe_local_path(target_dir, "../../etc/passwd")
        assert result is None

    def test_symlink_escape_blocked(self, tmp_path):
        ch = _make_channel(tmp_path)
        target_dir = tmp_path / "docs"
        target_dir.mkdir()
        # Create a symlink that points outside target_dir
        outside = tmp_path / "outside"
        outside.mkdir()
        link = target_dir / "sneaky_link"
        link.symlink_to(outside)
        # A file through the symlink resolves outside target_dir
        result = ch._safe_local_path(target_dir, "sneaky_link/payload.txt")
        assert result is None


# ===========================================================================
# Size limit enforcement
# ===========================================================================

def _mock_getfile_response(file_path: str, file_size: int):
    """Create a mock httpx response for Telegram's getFile API."""
    resp = MagicMock()
    resp.json.return_value = {
        "ok": True,
        "result": {"file_path": file_path, "file_size": file_size},
    }
    return resp


def _mock_download_response(content: bytes):
    """Create a mock httpx response for the actual file download."""
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    return resp


class TestDownloadDocumentSizeLimit:
    @pytest.mark.asyncio
    async def test_rejects_oversized_file_by_metadata(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        # getFile reports 60MB (over 50MB limit)
        ch._client = AsyncMock()
        ch._client.get = AsyncMock(return_value=_mock_getfile_response("docs/big.pdf", 60 * 1024 * 1024))

        result = await ch._download_document("file123", "big.pdf", "test_agent")
        assert result is None
        # Download should NOT have been attempted (only 1 call to getFile)
        assert ch._client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_rejects_oversized_file_by_content(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        # getFile reports 0 (unknown size) but actual content is huge
        big_content = b"x" * (51 * 1024 * 1024)
        ch._client = AsyncMock()
        ch._client.get = AsyncMock(side_effect=[
            _mock_getfile_response("docs/tricky.pdf", 0),
            _mock_download_response(big_content),
        ])

        result = await ch._download_document("file123", "tricky.pdf", "test_agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_allows_normal_sized_file(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        content = b"hello world"
        ch._client = AsyncMock()
        ch._client.get = AsyncMock(side_effect=[
            _mock_getfile_response("docs/small.pdf", len(content)),
            _mock_download_response(content),
        ])

        result = await ch._download_document("file123", "small.pdf", "test_agent")
        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_bytes() == content


class TestDownloadVoiceSizeLimit:
    @pytest.mark.asyncio
    async def test_rejects_oversized_voice(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        ch._client = AsyncMock()
        ch._client.get = AsyncMock(return_value=_mock_getfile_response("voice/big.ogg", 25 * 1024 * 1024))

        result = await ch._download_voice("file456", "test_agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_allows_normal_voice(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        content = b"audio data"
        ch._client = AsyncMock()
        ch._client.get = AsyncMock(side_effect=[
            _mock_getfile_response("voice/msg.ogg", len(content)),
            _mock_download_response(content),
        ])

        result = await ch._download_voice("file456", "test_agent")
        assert result is not None
        assert Path(result).exists()


class TestDownloadPhotoSizeLimit:
    @pytest.mark.asyncio
    async def test_rejects_oversized_photo(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        ch._client = AsyncMock()
        ch._client.get = AsyncMock(return_value=_mock_getfile_response("photos/big.jpg", 25 * 1024 * 1024))

        result = await ch._download_photo("file789", "test_agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_allows_normal_photo(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        content = b"image data"
        ch._client = AsyncMock()
        ch._client.get = AsyncMock(side_effect=[
            _mock_getfile_response("photos/pic.jpg", len(content)),
            _mock_download_response(content),
        ])

        result = await ch._download_photo("file789", "test_agent")
        assert result is not None
        assert Path(result).exists()


class TestDownloadDocumentSanitization:
    """End-to-end: malicious filenames are sanitized in _download_document."""

    @pytest.mark.asyncio
    async def test_path_traversal_filename_sanitized(self, tmp_path):
        ch = _make_channel(tmp_path)
        ch._user_data = MagicMock()
        ch._user_data.get_agent_tmp_dir.return_value = tmp_path

        content = b"payload"
        ch._client = AsyncMock()
        ch._client.get = AsyncMock(side_effect=[
            _mock_getfile_response("docs/file.pdf", len(content)),
            _mock_download_response(content),
        ])

        result = await ch._download_document("file123", "../../etc/passwd", "test_agent")
        assert result is not None
        # The file should be inside the documents dir, not at ../../etc/passwd
        assert "etc" not in result or "documents" in result
        result_path = Path(result)
        assert result_path.parent.name == "documents"
        assert result_path.name == "passwd"
