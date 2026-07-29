"""
Process utilities tests.
"""

from pathlib import Path
import tempfile
import os

import pytest

from src.everbot.infra.process import (
    DaemonAlreadyRunningError,
    DaemonLock,
    write_pid_file,
    read_pid_file,
    remove_pid_file,
    is_pid_running,
    rotate_file_if_large,
)


def test_pid_file_roundtrip():
    """PID file write/read/remove roundtrip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "everbot.pid"
        pid = write_pid_file(path, pid=12345)
        assert pid == 12345
        assert read_pid_file(path) == 12345
        remove_pid_file(path)
        assert read_pid_file(path) is None


def test_is_pid_running_current_process():
    """Current process PID should be running."""
    assert is_pid_running(os.getpid()) is True


def test_daemon_lock_second_acquire_raises_already_running(tmp_path: Path):
    """Second exclusive acquire must raise DaemonAlreadyRunningError, not bare RuntimeError."""
    lock_path = tmp_path / "everbot.lock"
    first = DaemonLock(lock_path)
    first.acquire()
    try:
        second = DaemonLock(lock_path)
        with pytest.raises(DaemonAlreadyRunningError) as exc_info:
            second.acquire()
        assert "already running" in str(exc_info.value).lower()
    finally:
        first.release()


def test_rotate_file_if_large_rotates_and_truncates(tmp_path: Path):
    """Oversized logs are copied to .1 and the live inode is truncated in place."""
    err = tmp_path / "everbot.err"
    err.write_bytes(b"x" * 100)
    live_inode = err.stat().st_ino
    rotated = rotate_file_if_large(err, max_bytes=50, backups=2)
    assert rotated is True
    assert err.exists()
    assert err.stat().st_size == 0
    assert err.stat().st_ino == live_inode
    assert (tmp_path / "everbot.err.1").exists()
    assert (tmp_path / "everbot.err.1").stat().st_size == 100


def test_rotate_file_if_large_noop_when_small(tmp_path: Path):
    """Files under the size limit are left untouched."""
    err = tmp_path / "everbot.err"
    err.write_text("ok\n", encoding="utf-8")
    rotated = rotate_file_if_large(err, max_bytes=50, backups=2)
    assert rotated is False
    assert err.read_text(encoding="utf-8") == "ok\n"
    assert not (tmp_path / "everbot.err.1").exists()
