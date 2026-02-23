"""
Process utilities tests.
"""

from pathlib import Path
import tempfile
import os

from src.everbot.infra.process import write_pid_file, read_pid_file, remove_pid_file, is_pid_running


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
