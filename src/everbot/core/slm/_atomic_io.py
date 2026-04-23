"""Atomic file IO + per-skill locking for SLM.

atomic_write_text uses tempfile + os.replace to ensure either the full new
content lands or the old file is untouched. os.replace is POSIX-atomic on
same filesystem.

skill_lock uses fcntl.flock on a .lock file in the skill's eval dir to
serialize concurrent writers (daemon vs CLI).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path
from typing import Iterator


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


@contextlib.contextmanager
def skill_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive per-skill file lock via fcntl.flock. Blocks until acquired."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
