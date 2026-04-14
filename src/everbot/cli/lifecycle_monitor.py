"""Detached monitor for daemon lifecycle forensics."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def _read_snapshot(path: Path) -> Dict[str, Any]:
    """Read lifecycle snapshot from disk."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def mark_unexpected_exit(
    lifecycle_file: Path,
    *,
    run_id: str,
    detected_at: str | None = None,
) -> bool:
    """Mark a daemon run as unexpectedly terminated if still unresolved."""
    snapshot = _read_snapshot(lifecycle_file)
    if not snapshot:
        return False
    if str(snapshot.get("run_id") or "").strip() != run_id:
        return False
    if bool(snapshot.get("graceful_shutdown", False)):
        return False
    if str(snapshot.get("status") or "").strip() not in {"starting", "running"}:
        return False

    now_iso = detected_at or datetime.now().isoformat()
    snapshot.update(
        {
            "status": "terminated",
            "updated_at": now_iso,
            "detected_dead_at": now_iso,
            "exit_reason": "monitor_detected_process_exit",
            "monitor_pid": os.getpid(),
        }
    )
    lifecycle_file.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_file.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def monitor_daemon(*, pid: int, run_id: str, lifecycle_file: Path, poll_seconds: float = 2.0) -> int:
    """Watch daemon PID and mark unexpected exit when it disappears."""
    while True:
        if pid <= 0:
            return 1
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            mark_unexpected_exit(lifecycle_file, run_id=run_id)
            return 0
        except PermissionError:
            pass
        time.sleep(poll_seconds)


def main() -> int:
    """CLI entrypoint for detached monitor."""
    parser = argparse.ArgumentParser(description="EverBot daemon lifecycle monitor")
    parser.add_argument("--pid", type=int, required=True, help="Daemon PID to monitor")
    parser.add_argument("--run-id", type=str, required=True, help="Daemon lifecycle run id")
    parser.add_argument("--lifecycle-file", type=str, required=True, help="Lifecycle snapshot file")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval")
    args = parser.parse_args()
    return monitor_daemon(
        pid=args.pid,
        run_id=args.run_id,
        lifecycle_file=Path(args.lifecycle_file).expanduser(),
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
