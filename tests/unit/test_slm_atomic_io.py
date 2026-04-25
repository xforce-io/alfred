"""Tests for atomic_write_text and skill_lock."""

import os
import threading
from pathlib import Path

import pytest

from src.everbot.core.slm._atomic_io import atomic_write_text, skill_lock


class TestAtomicWriteText:
    def test_creates_file_with_content(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello")
        assert target.read_text() == "hello"

    def test_overwrites_existing_atomically(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        target.write_text("old")
        atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_no_temp_file_leaks_on_success(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello")
        leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
        assert leftovers == []

    def test_parent_must_exist(self, tmp_path: Path):
        target = tmp_path / "missing_dir" / "out.txt"
        with pytest.raises(FileNotFoundError):
            atomic_write_text(target, "hello")

    def test_no_temp_file_leaks_on_write_failure(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "out.txt"
        target.write_text("original")

        def fail_fsync(fd):
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="simulated fsync failure"):
            atomic_write_text(target, "new content")

        # Target must still contain original content (atomicity guarantee)
        assert target.read_text() == "original"
        # No .tmp leftovers in dir
        leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
        assert leftovers == [], f"tempfile leaked: {leftovers}"


class TestSkillLock:
    def test_serializes_concurrent_writers(self, tmp_path: Path):
        lock_path = tmp_path / ".lock"
        counter = {"value": 0}
        errors: list = []

        def worker():
            try:
                with skill_lock(lock_path):
                    seen = counter["value"]
                    # Window where a second worker could interleave if lock broken
                    counter["value"] = seen + 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert counter["value"] == 20

    def test_lock_file_is_created_in_missing_dir(self, tmp_path: Path):
        lock_path = tmp_path / "nested" / "dirs" / ".lock"
        with skill_lock(lock_path):
            pass
        assert lock_path.exists()
