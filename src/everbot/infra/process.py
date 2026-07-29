"""
Process utilities.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when another EverBot daemon already holds the singleton lock.

    Callers that implement launchd KeepAlive should treat this as a successful
    no-op (exit 0) so the service manager does not thrash restart loops.
    """


class DaemonLock:
    """File-lock based singleton guard for the daemon process.

    Acquire the lock before starting the daemon. The lock is held for the
    lifetime of the process and automatically released on exit / crash.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        """Acquire an exclusive lock.

        Raises:
            DaemonAlreadyRunningError: another daemon instance already holds the lock.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise DaemonAlreadyRunningError(
                f"Another EverBot daemon is already running (lock: {self._lock_path})"
            )

    def release(self) -> None:
        """Release the lock and remove the lock file."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass


def write_pid_file(path: Path, pid: Optional[int] = None) -> int:
    """
    Write a PID file and return the PID written.

    Args:
        path: PID file path.
        pid: PID to write. Defaults to current process PID.
    """
    actual_pid = int(pid or os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{actual_pid}\n", encoding="utf-8")
    return actual_pid


def read_pid_file(path: Path) -> Optional[int]:
    """Read PID from file. Returns None if missing/invalid."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def remove_pid_file(path: Path) -> None:
    """Remove PID file if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def is_pid_running(pid: int) -> bool:
    """
    Check whether a PID is currently running.

    Note: This only checks process existence, not whether it's the expected daemon.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def rotate_file_if_large(
    path: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 3,
) -> bool:
    """Rotate ``path`` when it exceeds ``max_bytes``.

    Copies the live file to ``.1`` (shifting older backups) and **truncates the
    original inode in place**. launchd keeps StandardErrorPath FDs open across
    exec, so rename-and-recreate would leave writers attached to the backup.
    Returns True if a rotation happened.
    """
    if max_bytes <= 0:
        return False
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if size <= max_bytes:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    # Drop the oldest backup first, then shift .N -> .(N+1).
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            pass
    for index in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            try:
                src.replace(dst)
            except OSError:
                pass

    backup = path.with_name(f"{path.name}.1")
    try:
        # Copy then truncate the same inode so open FDs keep writing the live path.
        with path.open("rb") as src_fh:
            payload = src_fh.read()
        with backup.open("wb") as dst_fh:
            dst_fh.write(payload)
        with path.open("r+b") as live_fh:
            live_fh.truncate(0)
    except OSError:
        return False
    return True
