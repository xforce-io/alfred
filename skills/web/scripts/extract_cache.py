"""Content-addressed cache for web extract full text.

Stores normalized UTF-8 page bodies under a governed local directory.
Paths are derived only from SHA-256 hex; callers never receive absolute paths
in public APIs. Concurrent writers use atomic rename; capacity is enforced
with LRU eviction (entries currently pinned for I/O are not evicted).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_CACHE_MAX_BYTES = 128 * 1024 * 1024  # 128 MiB
MAX_READ_LIMIT = 6000
CONTENT_ID_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class CacheError(Exception):
    """Base cache error (messages must not leak absolute paths)."""


class CacheFullError(CacheError):
    """Raised when the cache cannot free enough space to store new content."""

    code = "cache_full"


class CacheUnavailableError(CacheError):
    """Raised when the cache root is not usable (permissions, etc.)."""

    code = "cache_unavailable"


class InvalidContentIdError(CacheError):
    """Raised when content_id fails strict validation."""

    code = "invalid_content_id"


class NotFoundError(CacheError):
    """Raised when content_id is valid but not present (or corrupted)."""

    code = "not_found"


class InvalidRangeError(CacheError):
    """Raised for illegal offset/limit values."""

    code = "invalid_range"


def normalize_text(text: str) -> str:
    """Normalize extract body: collapse whitespace, strip edges."""
    return " ".join(text.split())


def content_hash_hex(text: str) -> str:
    """Return SHA-256 hex of normalized UTF-8 bytes."""
    data = normalize_text(text).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def format_content_id(hex_digest: str) -> str:
    """Format as sha256:<hex>."""
    return f"sha256:{hex_digest}"


def parse_content_id(content_id: str) -> str:
    """Parse and validate content_id; return 64-char lowercase hex.

    Rejects paths, uppercase-only mixed garbage, non-hex, wrong length.
    """
    if not isinstance(content_id, str) or not content_id:
        raise InvalidContentIdError("invalid content_id")
    # Reject path-like input early
    if "/" in content_id or "\\" in content_id or ".." in content_id:
        raise InvalidContentIdError("invalid content_id")
    match = CONTENT_ID_RE.match(content_id.strip())
    if not match:
        raise InvalidContentIdError("invalid content_id")
    return match.group(1)


def default_cache_root() -> Path:
    env = os.environ.get("ALFRED_WEB_EXTRACT_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".alfred" / "cache" / "web-extract"


def default_max_bytes() -> int:
    env = os.environ.get("ALFRED_WEB_EXTRACT_CACHE_MAX_BYTES")
    if env:
        try:
            return int(env)
        except ValueError:
            return DEFAULT_CACHE_MAX_BYTES
    return DEFAULT_CACHE_MAX_BYTES


class ExtractCache:
    """Content-addressed extract body store with LRU capacity control."""

    def __init__(
        self,
        root: Path | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_cache_root()
        self.max_bytes = max_bytes if max_bytes is not None else default_max_bytes()
        self._lock = threading.RLock()
        self._pins: dict[str, int] = {}  # hex -> pin count
        self._ensure_root()

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise CacheUnavailableError("cache unavailable") from exc

    def _path_for_hex(self, hex_digest: str) -> Path:
        # Only hex segments — never user-controlled relative paths.
        return self.root / hex_digest[0:2] / hex_digest[2:4] / f"{hex_digest}.txt"

    def _pin(self, hex_digest: str) -> None:
        self._pins[hex_digest] = self._pins.get(hex_digest, 0) + 1

    def _unpin(self, hex_digest: str) -> None:
        count = self._pins.get(hex_digest, 0)
        if count <= 1:
            self._pins.pop(hex_digest, None)
        else:
            self._pins[hex_digest] = count - 1

    def _is_pinned(self, hex_digest: str) -> bool:
        return self._pins.get(hex_digest, 0) > 0

    def _iter_entries(self) -> list[tuple[str, Path, int, float]]:
        """Return (hex, path, size, atime) for all body files."""
        entries: list[tuple[str, Path, int, float]] = []
        if not self.root.exists():
            return entries
        for path in self.root.rglob("*.txt"):
            name = path.name
            if not name.endswith(".txt"):
                continue
            hex_digest = name[:-4]
            if not re.fullmatch(r"[0-9a-f]{64}", hex_digest):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((hex_digest, path, st.st_size, st.st_atime))
        return entries

    def _total_bytes(self) -> int:
        return sum(size for _, _, size, _ in self._iter_entries())

    def _touch(self, path: Path) -> None:
        now = time.time()
        try:
            os.utime(path, (now, now))
        except OSError:
            pass

    def _evict_until(self, need_bytes: int) -> None:
        """Evict LRU unpinned entries until free space can hold need_bytes."""
        while True:
            total = self._total_bytes()
            if total + need_bytes <= self.max_bytes:
                return
            candidates = [
                e
                for e in self._iter_entries()
                if not self._is_pinned(e[0])
            ]
            if not candidates:
                raise CacheFullError("cache_full")
            # Oldest access time first
            candidates.sort(key=lambda e: e[3])
            victim_hex, victim_path, _, _ = candidates[0]
            try:
                victim_path.unlink(missing_ok=True)
                # Clean empty shard dirs (best-effort)
                parent = victim_path.parent
                try:
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                    grand = parent.parent
                    if grand.is_dir() and not any(grand.iterdir()) and grand != self.root:
                        grand.rmdir()
                except OSError:
                    pass
            except OSError:
                # If we cannot unlink, treat as pinned/unavailable for eviction
                raise CacheFullError("cache_full")

    def store(self, text: str) -> str:
        """Normalize UTF-8, SHA-256, atomic write; return content_id.

        On hash hit: touch access time and return existing id.
        Raises CacheFullError | CacheUnavailableError.
        """
        if not isinstance(text, str):
            raise CacheError("text must be str")
        normalized = normalize_text(text)
        if not normalized:
            raise CacheError("empty text")

        data = normalized.encode("utf-8")
        hex_digest = hashlib.sha256(data).hexdigest()
        content_id = format_content_id(hex_digest)
        path = self._path_for_hex(hex_digest)

        with self._lock:
            self._ensure_root()
            self._pin(hex_digest)
            try:
                if path.is_file():
                    # Verify integrity; repair if corrupt
                    try:
                        existing = path.read_bytes()
                        if hashlib.sha256(existing).hexdigest() == hex_digest:
                            self._touch(path)
                            return content_id
                        path.unlink(missing_ok=True)
                    except OSError:
                        path.unlink(missing_ok=True)

                need = len(data)
                if need > self.max_bytes:
                    raise CacheFullError("cache_full")
                self._evict_until(need)

                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # Ensure shard dirs are restrictive when created under root
                    for p in (path.parent.parent, path.parent):
                        try:
                            os.chmod(p, 0o700)
                        except OSError:
                            pass

                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".{hex_digest}.",
                        suffix=".tmp",
                        dir=str(path.parent),
                    )
                    try:
                        with os.fdopen(fd, "wb") as tmp:
                            tmp.write(data)
                            tmp.flush()
                            os.fsync(tmp.fileno())
                        os.chmod(tmp_name, 0o600)
                        os.replace(tmp_name, path)
                        os.chmod(path, 0o600)
                    except Exception:
                        try:
                            os.unlink(tmp_name)
                        except OSError:
                            pass
                        raise

                    # Post-write hash check
                    written = path.read_bytes()
                    if hashlib.sha256(written).hexdigest() != hex_digest:
                        path.unlink(missing_ok=True)
                        raise CacheUnavailableError("cache unavailable")
                    self._touch(path)
                    return content_id
                except CacheError:
                    raise
                except OSError as exc:
                    raise CacheUnavailableError("cache unavailable") from exc
            finally:
                self._unpin(hex_digest)

    def read_range(self, content_id: str, offset: int, limit: int) -> str:
        """Return text[offset:offset+limit] by Unicode character index."""
        if not isinstance(offset, int) or offset < 0:
            raise InvalidRangeError("invalid range")
        if not isinstance(limit, int) or limit < 1 or limit > MAX_READ_LIMIT:
            raise InvalidRangeError("invalid range")

        hex_digest = parse_content_id(content_id)
        path = self._path_for_hex(hex_digest)

        with self._lock:
            self._pin(hex_digest)
            try:
                if not path.is_file():
                    raise NotFoundError("not found")
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise CacheUnavailableError("cache unavailable") from exc

                if hashlib.sha256(raw).hexdigest() != hex_digest:
                    path.unlink(missing_ok=True)
                    raise NotFoundError("not found")

                text = raw.decode("utf-8")
                self._touch(path)
                return text[offset : offset + limit]
            finally:
                self._unpin(hex_digest)

    def full_char_len(self, content_id: str) -> int:
        """Return Unicode character length of stored body."""
        hex_digest = parse_content_id(content_id)
        path = self._path_for_hex(hex_digest)
        with self._lock:
            self._pin(hex_digest)
            try:
                if not path.is_file():
                    raise NotFoundError("not found")
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise CacheUnavailableError("cache unavailable") from exc
                if hashlib.sha256(raw).hexdigest() != hex_digest:
                    path.unlink(missing_ok=True)
                    raise NotFoundError("not found")
                text = raw.decode("utf-8")
                self._touch(path)
                return len(text)
            finally:
                self._unpin(hex_digest)

    def full_text(self, content_id: str) -> str:
        """Return full stored text (for tests / internal use)."""
        hex_digest = parse_content_id(content_id)
        path = self._path_for_hex(hex_digest)
        with self._lock:
            self._pin(hex_digest)
            try:
                if not path.is_file():
                    raise NotFoundError("not found")
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != hex_digest:
                    path.unlink(missing_ok=True)
                    raise NotFoundError("not found")
                self._touch(path)
                return raw.decode("utf-8")
            finally:
                self._unpin(hex_digest)
