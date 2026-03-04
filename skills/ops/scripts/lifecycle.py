"""Lifecycle management: start, stop, restart via bin/everbot."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from observe import _read_status_snapshot, _read_pid, _is_pid_running


def _find_everbot_bin(alfred_home: Path) -> Optional[str]:
    """Locate bin/everbot via project_root from status snapshot."""
    snapshot = _read_status_snapshot(alfred_home)
    if snapshot and snapshot.get("project_root"):
        candidate = Path(snapshot["project_root"]) / "bin" / "everbot"
        if candidate.exists():
            return str(candidate)
    return None


def _run_everbot(everbot_bin: str, args: list[str], timeout: int = 30) -> Dict[str, Any]:
    """Execute bin/everbot with given arguments."""
    cmd = [everbot_bin] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"command timed out after {timeout}s"}
    except OSError as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


def cmd_start(alfred_home: Path) -> Dict[str, Any]:
    """Start the daemon."""
    # Check if already running
    pid = _read_pid(alfred_home)
    if pid and _is_pid_running(pid):
        return {"ok": True, "command": "start",
                "data": {"message": f"daemon already running (pid={pid})", "pid": pid}}

    everbot_bin = _find_everbot_bin(alfred_home)
    if not everbot_bin:
        return {"ok": False, "command": "start",
                "error": "cannot locate bin/everbot",
                "hint": "project_root not found in status snapshot. Start the daemon manually first."}

    result = _run_everbot(everbot_bin, ["start"])
    if result["exit_code"] == 0:
        return {"ok": True, "command": "start",
                "data": {"message": "daemon started", "output": result["stdout"]}}
    return {"ok": False, "command": "start",
            "error": f"start failed (exit={result['exit_code']})",
            "detail": result["stderr"] or result["stdout"]}


def cmd_stop(alfred_home: Path) -> Dict[str, Any]:
    """Stop the daemon."""
    everbot_bin = _find_everbot_bin(alfred_home)
    if not everbot_bin:
        # Try to stop by PID directly
        pid = _read_pid(alfred_home)
        if not pid or not _is_pid_running(pid):
            return {"ok": True, "command": "stop",
                    "data": {"message": "daemon not running"}}
        return {"ok": False, "command": "stop",
                "error": "cannot locate bin/everbot to stop gracefully",
                "hint": "project_root not found in status snapshot"}

    result = _run_everbot(everbot_bin, ["stop"])
    if result["exit_code"] == 0:
        return {"ok": True, "command": "stop",
                "data": {"message": "daemon stopped", "output": result["stdout"]}}
    return {"ok": False, "command": "stop",
            "error": f"stop failed (exit={result['exit_code']})",
            "detail": result["stderr"] or result["stdout"]}


def cmd_restart(alfred_home: Path) -> Dict[str, Any]:
    """Restart the daemon."""
    everbot_bin = _find_everbot_bin(alfred_home)
    if not everbot_bin:
        return {"ok": False, "command": "restart",
                "error": "cannot locate bin/everbot",
                "hint": "project_root not found in status snapshot"}

    result = _run_everbot(everbot_bin, ["restart"], timeout=60)
    if result["exit_code"] == 0:
        return {"ok": True, "command": "restart",
                "data": {"message": "daemon restarted", "output": result["stdout"]}}
    return {"ok": False, "command": "restart",
            "error": f"restart failed (exit={result['exit_code']})",
            "detail": result["stderr"] or result["stdout"]}
