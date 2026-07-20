"""CLI tests for read_extract.py (I6, I7)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from extract_cache import ExtractCache, MAX_READ_LIMIT
import read_extract


def test_i6_invalid_args_exit_nonzero(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    assert read_extract.main(["--content-id", "../x", "--offset", "0", "--limit", "10"]) == 2
    assert read_extract.main(["--content-id", "a" * 64, "--offset", "-1", "--limit", "10"]) == 2
    assert read_extract.main(["--content-id", "a" * 64, "--offset", "0", "--limit", "0"]) == 2
    assert (
        read_extract.main(
            ["--content-id", "a" * 64, "--offset", "0", "--limit", str(MAX_READ_LIMIT + 1)]
        )
        == 2
    )


def test_i6_unknown_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    code = read_extract.main(
        ["--content-id", "b" * 64, "--offset", "0", "--limit", "100", "--output", "json"]
    )
    assert code == 1


def test_i7_cli_paged_restore(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    cache = ExtractCache(root=tmp_path / "c", max_bytes=50 * 1024 * 1024)
    body = ("PAGE-CONTENT-" * 500)  # long enough
    assert len(body) > 5000
    cid = cache.store(body)
    hex_id = cid.split(":")[1]

    parts: list[str] = []
    offset = 0
    while True:
        capsys.readouterr()
        code = read_extract.main(
            [
                "--content-id",
                hex_id,
                "--offset",
                str(offset),
                "--limit",
                str(MAX_READ_LIMIT),
                "--output",
                "json",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["chars_returned"] <= MAX_READ_LIMIT
        assert "path" not in json.dumps(payload).lower() or "/Users" not in out
        assert str(tmp_path) not in out
        parts.append(payload["text"])
        if payload["eof"]:
            break
        offset += payload["chars_returned"]
        if payload["chars_returned"] == 0:
            break

    joined = "".join(parts)
    assert joined == cache.full_text(cid)


def test_text_output_is_pure_fragment(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_WEB_EXTRACT_CACHE_DIR", str(tmp_path / "c"))
    cache = ExtractCache(root=tmp_path / "c", max_bytes=1024 * 1024)
    body = "exact fragment body"
    cid = cache.store(body)
    code = read_extract.main(
        ["--content-id", cid, "--offset", "0", "--limit", "100", "--output", "text"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.strip() == body
