"""Atomic file IO + per-skill locking for SLM.

atomic_write_text uses tempfile + os.replace to ensure either the full new
content lands or the old file is untouched. os.replace is POSIX-atomic on
same filesystem.

skill_lock uses fcntl.flock on a .lock file in the skill's eval dir to
serialize concurrent writers (daemon vs CLI).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional


def atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write `content` to `target` atomically. Parent dir must exist."""
    if not target.parent.exists():
        raise FileNotFoundError(f"Parent directory missing: {target.parent}")
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise


class LockTimeoutError(Exception):
    """Raised when skill_lock cannot be acquired within timeout."""
    pass


@contextlib.contextmanager
def skill_lock(lock_path: Path, *, timeout: Optional[float] = None) -> Iterator[None]:
    """Exclusive per-skill file lock via fcntl.flock.

    Args:
        lock_path: Path to the lock file.
        timeout: Max seconds to wait for lock. None = block indefinitely (legacy).
                 When set, uses LOCK_NB polling with timeout (S1 requirement).

    Raises:
        LockTimeoutError: When timeout is set and lock cannot be acquired.

    Not re-entrant: do not nest two skill_lock contexts on the same path
    within the same thread (safe on Linux via fd semantics, deadlocks on
    strict BSD flock).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a")
    try:
        if timeout is None:
            # Legacy blocking mode
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            # LOCK_NB + polling mode (S1)
            deadline = time.monotonic() + timeout
            poll_interval = 0.05  # 50ms polling
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(
                            f"Could not acquire lock {lock_path} within {timeout}s"
                        )
                    time.sleep(poll_interval)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


@contextlib.asynccontextmanager
async def async_skill_lock(lock_path: Path, *, timeout: float = 5.0):
    """Async version of skill_lock with LOCK_NB polling (S1 requirement).

    Uses asyncio.sleep for polling to avoid blocking the event loop.
    Default timeout is 5s per design requirement.

    Raises:
        LockTimeoutError: When lock cannot be acquired within timeout.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a")
    try:
        deadline = time.monotonic() + timeout
        poll_interval = 0.05  # 50ms polling
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Could not acquire lock {lock_path} within {timeout}s"
                    )
                await asyncio.sleep(poll_interval)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()
