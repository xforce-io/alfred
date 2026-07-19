"""Integration/unit tests for ExtractCache (I1–I5, I7–I8, U5, U8)."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from extract_cache import (
    MAX_READ_LIMIT,
    CacheFullError,
    ExtractCache,
    InvalidContentIdError,
    InvalidRangeError,
    NotFoundError,
    content_hash_hex,
    format_content_id,
    parse_content_id,
)


def test_u5_content_id_format_and_stability(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=10 * 1024 * 1024)
    body = "Hello world extract body " * 50
    cid1 = cache.store(body)
    cid2 = cache.store(body)
    assert cid1 == cid2
    assert cid1.startswith("sha256:")
    hex_part = cid1.split(":", 1)[1]
    assert len(hex_part) == 64
    assert all(c in "0123456789abcdef" for c in hex_part)
    expected = content_hash_hex(body)
    assert hex_part == expected


def test_i1_dedupe_single_file(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=10 * 1024 * 1024)
    body = "same content " * 100
    cid = cache.store(body)
    cache.store(body)
    hex_digest = cid.split(":")[1]
    files = list((tmp_path / "c").rglob("*.txt"))
    assert len(files) == 1
    assert files[0].name == f"{hex_digest}.txt"


def test_i3_permissions_unix(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("unix permissions")
    root = tmp_path / "c"
    cache = ExtractCache(root=root, max_bytes=10 * 1024 * 1024)
    cid = cache.store("permission body text")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    hex_digest = cid.split(":")[1]
    path = root / hex_digest[0:2] / hex_digest[2:4] / f"{hex_digest}.txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_i4_lru_eviction(tmp_path: Path):
    # Small cache: each body ~200 bytes
    cache = ExtractCache(root=tmp_path / "c", max_bytes=500)
    ids = []
    for i in range(5):
        body = f"entry-{i}-" + ("x" * 180)
        ids.append(cache.store(body))
    # Older entries should be gone; latest should exist
    with pytest.raises(NotFoundError):
        cache.full_char_len(ids[0])
    assert cache.full_char_len(ids[-1]) > 0


def test_i4_read_updates_lru(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=450)
    a = cache.store("A" * 200)
    b = cache.store("B" * 200)
    # Touch A so B is older
    cache.read_range(a, 0, 10)
    # New entry should prefer evicting B
    c = cache.store("C" * 200)
    assert cache.full_char_len(a) > 0
    assert cache.full_char_len(c) > 0
    with pytest.raises(NotFoundError):
        cache.full_char_len(b)


def test_i5_cache_full_when_all_pinned(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=300)
    body_a = "A" * 200
    cid = cache.store(body_a)
    hex_a = cid.split(":")[1]
    # Pin A so it cannot be evicted
    cache._pin(hex_a)
    try:
        with pytest.raises(CacheFullError) as exc:
            cache.store("B" * 200)
        assert exc.value.code == "cache_full"
    finally:
        cache._unpin(hex_a)


def test_i2_no_half_file_on_failed_write(tmp_path: Path, monkeypatch):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=10 * 1024 * 1024)
    body = "atomic write body " * 20
    hex_digest = content_hash_hex(body)
    path = cache._path_for_hex(hex_digest)

    real_replace = os.replace

    def boom_replace(src, dst):
        # Simulate crash after temp write but before replace completes:
        # leave temp, fail replace — final path must not appear with valid id.
        raise OSError("simulated interrupt")

    monkeypatch.setattr(os, "replace", boom_replace)
    with pytest.raises((OSError, Exception)):
        # store wraps OSError as CacheUnavailableError
        try:
            cache.store(body)
        except Exception:
            raise
    # No committed body file, or if any tmp remains it is not a valid content path
    assert not path.is_file()
    valid_txt = [p for p in (tmp_path / "c").rglob("*.txt") if p.name == f"{hex_digest}.txt"]
    assert valid_txt == []


def test_i7_paged_read_roundtrip(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=50 * 1024 * 1024)
    body = "".join(f"Line {i} content pad " for i in range(3000))  # >30k
    assert len(body) > 30_000
    cid = cache.store(body)
    stored = cache.full_text(cid)
    chunks: list[str] = []
    offset = 0
    while offset < len(stored):
        page = cache.read_range(cid, offset, MAX_READ_LIMIT)
        assert len(page) <= MAX_READ_LIMIT
        chunks.append(page)
        if not page:
            break
        offset += len(page)
    joined = "".join(chunks)
    assert joined == stored
    assert hashlib.sha256(joined.encode("utf-8")).hexdigest() == cid.split(":")[1]


def test_i8_concurrent_store_same_body(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=10 * 1024 * 1024)
    body = "concurrent identical body " * 100
    results: list[str] = []

    def worker():
        results.append(cache.store(body))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(worker) for _ in range(8)]
        for f in futs:
            f.result()
    assert len(set(results)) == 1
    files = list((tmp_path / "c").rglob("*.txt"))
    assert len(files) == 1


def test_u8_parse_content_id_rejects_paths():
    with pytest.raises(InvalidContentIdError):
        parse_content_id("../etc/passwd")
    with pytest.raises(InvalidContentIdError):
        parse_content_id("/tmp/foo")
    with pytest.raises(InvalidContentIdError):
        parse_content_id("sha256:nothex")
    with pytest.raises(InvalidContentIdError):
        parse_content_id("abc")
    good = "a" * 64
    assert parse_content_id(good) == good
    assert parse_content_id(f"sha256:{good}") == good


def test_read_range_invalid(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=1024 * 1024)
    cid = cache.store("hello world")
    with pytest.raises(InvalidRangeError):
        cache.read_range(cid, -1, 10)
    with pytest.raises(InvalidRangeError):
        cache.read_range(cid, 0, 0)
    with pytest.raises(InvalidRangeError):
        cache.read_range(cid, 0, MAX_READ_LIMIT + 1)


def test_corrupt_file_deleted_on_read(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=1024 * 1024)
    cid = cache.store("good body text")
    hex_digest = cid.split(":")[1]
    path = cache._path_for_hex(hex_digest)
    path.write_bytes(b"corrupted-not-matching-hash")
    with pytest.raises(NotFoundError):
        cache.read_range(cid, 0, 10)
    assert not path.is_file()


def test_offset_past_end_returns_empty(tmp_path: Path):
    cache = ExtractCache(root=tmp_path / "c", max_bytes=1024 * 1024)
    cid = cache.store("short")
    assert cache.read_range(cid, 100, 10) == ""


def test_format_content_id():
    assert format_content_id("a" * 64) == "sha256:" + "a" * 64
