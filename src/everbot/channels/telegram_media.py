"""Telegram media extraction and secure file download utilities."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Size limits (bytes)
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024   # 50 MB
MAX_VOICE_SIZE = 20 * 1024 * 1024      # 20 MB
MAX_PHOTO_SIZE = 20 * 1024 * 1024      # 20 MB


def extract_media_text(msg: dict, extract_urls_fn) -> str:
    """Extract a structured text description from a media message.

    *extract_urls_fn* is the module-level ``_extract_urls`` helper so that
    this module stays free of circular imports.
    """
    parts: list[str] = []
    caption = (msg.get("caption") or "").strip()

    if msg.get("voice"):
        v = msg["voice"]
        parts.append(f"[语音消息 duration={v.get('duration', 0)}s]")
    if msg.get("audio"):
        a = msg["audio"]
        info = a.get("title") or a.get("file_name") or ""
        parts.append(f"[音频: {info} duration={a.get('duration', 0)}s]")
    if msg.get("photo"):
        parts.append("[图片]")
    if msg.get("video"):
        v = msg["video"]
        parts.append(f"[视频 duration={v.get('duration', 0)}s]")
    if msg.get("document"):
        d = msg["document"]
        fname = d.get("file_name") or "unknown"
        mime = d.get("mime_type") or ""
        parts.append(f"[文件: {fname} ({mime})]" if mime else f"[文件: {fname}]")
    if msg.get("sticker"):
        s = msg["sticker"]
        parts.append(f"[贴纸: {s.get('emoji', '')}]")

    urls = extract_urls_fn(caption, msg.get("caption_entities") or [])

    tag = " ".join(parts)
    pieces = [p for p in [tag, caption] if p]
    for u in urls:
        if u not in caption:
            pieces.append(u)

    return "\n".join(pieces).strip()


# ------------------------------------------------------------------
# Security helpers
# ------------------------------------------------------------------

def sanitize_filename(raw: str) -> str:
    """Strip path components and special characters to prevent path traversal."""
    name = Path(raw).name                              # drop directory parts
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)     # keep only safe chars
    return name.strip("._-") or "unnamed"


def safe_local_path(target_dir: Path, filename: str) -> Optional[Path]:
    """Resolve the final path and verify it stays inside *target_dir*."""
    candidate = (target_dir / filename).resolve()
    try:
        candidate.relative_to(target_dir.resolve())
    except ValueError:
        logger.warning("Path traversal attempt blocked: %s", filename)
        return None
    return candidate


# ------------------------------------------------------------------
# Download helpers
# ------------------------------------------------------------------

async def _get_telegram_file(
    client: httpx.AsyncClient,
    base_url: str,
    file_id: str,
) -> Optional[dict]:
    """Call getFile and return result dict, or None on failure."""
    resp = await client.get(f"{base_url}/getFile", params={"file_id": file_id})
    data = resp.json()
    if not data.get("ok"):
        logger.warning("getFile failed for %s: %s", file_id, data.get("description"))
        return None
    return data["result"]


async def _download_bytes(
    client: httpx.AsyncClient,
    file_base_url: str,
    remote_path: str,
    max_size: int,
    file_id: str,
    label: str,
) -> Optional[bytes]:
    """Download file bytes, enforcing *max_size*."""
    download_url = f"{file_base_url}/{remote_path}"
    resp = await client.get(download_url)
    resp.raise_for_status()
    if len(resp.content) > max_size:
        logger.warning(
            "%s %s download size exceeds limit (%d), discarding",
            label, file_id, len(resp.content),
        )
        return None
    return resp.content


async def download_document(
    client: httpx.AsyncClient,
    base_url: str,
    file_base_url: str,
    file_id: str,
    file_name: str,
    target_dir: Path,
) -> Optional[str]:
    """Download a Telegram document file and return the local path."""
    if not file_id or client is None:
        return None
    try:
        file_info = await _get_telegram_file(client, base_url, file_id)
        if file_info is None:
            return None

        file_size = file_info.get("file_size", 0)
        if file_size and file_size > MAX_DOCUMENT_SIZE:
            logger.warning(
                "Document %s exceeds size limit (%d > %d), skipping",
                file_id, file_size, MAX_DOCUMENT_SIZE,
            )
            return None

        content = await _download_bytes(
            client, file_base_url, file_info["file_path"],
            MAX_DOCUMENT_SIZE, file_id, "Document",
        )
        if content is None:
            return None

        doc_dir = target_dir / "documents"
        doc_dir.mkdir(parents=True, exist_ok=True)
        raw_name = file_name or Path(file_info["file_path"]).name or f"{file_id}"
        safe_name = sanitize_filename(raw_name)
        local_path = safe_local_path(doc_dir, safe_name)
        if local_path is None:
            return None
        local_path.write_bytes(content)
        return str(local_path)
    except Exception as exc:
        logger.error("Failed to download document %s: %s", file_id, exc)
        return None


async def download_voice(
    client: httpx.AsyncClient,
    base_url: str,
    file_base_url: str,
    file_id: str,
    target_dir: Path,
) -> Optional[str]:
    """Download a Telegram voice file and return the local path."""
    if not file_id or client is None:
        return None
    try:
        file_info = await _get_telegram_file(client, base_url, file_id)
        if file_info is None:
            return None

        file_size = file_info.get("file_size", 0)
        if file_size and file_size > MAX_VOICE_SIZE:
            logger.warning(
                "Voice %s exceeds size limit (%d > %d), skipping",
                file_id, file_size, MAX_VOICE_SIZE,
            )
            return None

        remote_path = file_info["file_path"]
        content = await _download_bytes(
            client, file_base_url, remote_path,
            MAX_VOICE_SIZE, file_id, "Voice",
        )
        if content is None:
            return None

        voice_dir = target_dir / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(remote_path).suffix or ".ogg"
        safe_name = sanitize_filename(f"{file_id}{suffix}")
        local_path = safe_local_path(voice_dir, safe_name)
        if local_path is None:
            return None
        local_path.write_bytes(content)
        return str(local_path)
    except Exception as exc:
        logger.error("Failed to download voice file %s: %s", file_id, exc)
        return None


async def download_photo(
    client: httpx.AsyncClient,
    base_url: str,
    file_base_url: str,
    file_id: str,
    target_dir: Path,
) -> Optional[str]:
    """Download a Telegram photo and return the local path."""
    if not file_id or client is None:
        return None
    try:
        file_info = await _get_telegram_file(client, base_url, file_id)
        if file_info is None:
            return None

        file_size = file_info.get("file_size", 0)
        if file_size and file_size > MAX_PHOTO_SIZE:
            logger.warning(
                "Photo %s exceeds size limit (%d > %d), skipping",
                file_id, file_size, MAX_PHOTO_SIZE,
            )
            return None

        remote_path = file_info["file_path"]
        content = await _download_bytes(
            client, file_base_url, remote_path,
            MAX_PHOTO_SIZE, file_id, "Photo",
        )
        if content is None:
            return None

        photo_dir = target_dir / "photos"
        photo_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(remote_path).suffix or ".jpg"
        safe_name = sanitize_filename(f"{file_id}{suffix}")
        local_path = safe_local_path(photo_dir, safe_name)
        if local_path is None:
            return None
        local_path.write_bytes(content)
        return str(local_path)
    except Exception as exc:
        logger.error("Failed to download photo %s: %s", file_id, exc)
        return None
