#!/usr/bin/env python3
"""Ops skill CLI dispatcher — operations and observability for Alfred daemon."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _setup_import_path() -> None:
    """Add the skill scripts directory to sys.path for relative imports."""
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _print_result(result: Dict[str, Any]) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alfred ops CLI")
    parser.add_argument(
        "--alfred-home", default="~/.alfred",
        help="Path to ALFRED_HOME (default: ~/.alfred)",
    )

    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="Daemon running state, PID, uptime, agents")

    # heartbeat
    hb_p = sub.add_parser("heartbeat", help="Agent heartbeat status")
    hb_p.add_argument("--agent", default=None, help="Filter by agent name")

    # tasks
    tasks_p = sub.add_parser("tasks", help="Agent task list from HEARTBEAT.md")
    tasks_p.add_argument("--agent", required=True, help="Agent name")

    # logs
    logs_p = sub.add_parser("logs", help="Read recent log lines")
    logs_p.add_argument("--source", default="heartbeat",
                        choices=["daemon", "heartbeat", "web"],
                        help="Log source (default: heartbeat)")
    logs_p.add_argument("--tail", type=int, default=50, help="Number of lines (default: 50)")
    logs_p.add_argument("--level", default=None, help="Filter by log level (ERROR, WARNING, INFO)")
    logs_p.add_argument("--agent", default=None, help="Filter by agent name")

    # metrics
    sub.add_parser("metrics", help="Runtime metrics from status snapshot")

    # diagnose
    diag_p = sub.add_parser("diagnose", help="Comprehensive health diagnostics")
    diag_p.add_argument("--agent", default=None, help="Focus on a specific agent")

    # lifecycle
    sub.add_parser("start", help="Start the daemon")
    sub.add_parser("stop", help="Stop the daemon")
    sub.add_parser("restart", help="Restart the daemon")

    return parser


def main() -> int:
    _setup_import_path()
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    alfred_home = Path(args.alfred_home).expanduser()

    try:
        if args.command == "status":
            from observe import cmd_status
            return _print_result(cmd_status(alfred_home))

        if args.command == "heartbeat":
            from observe import cmd_heartbeat
            return _print_result(cmd_heartbeat(alfred_home, agent=args.agent))

        if args.command == "tasks":
            from observe import cmd_tasks
            return _print_result(cmd_tasks(alfred_home, agent=args.agent))

        if args.command == "logs":
            from observe import cmd_logs
            return _print_result(cmd_logs(
                alfred_home, source=args.source, tail=args.tail,
                level=args.level, agent=args.agent,
            ))

        if args.command == "metrics":
            from observe import cmd_metrics
            return _print_result(cmd_metrics(alfred_home))

        if args.command == "diagnose":
            from diagnose import cmd_diagnose
            return _print_result(cmd_diagnose(alfred_home, agent=args.agent))

        if args.command in ("start", "stop", "restart"):
            from lifecycle import cmd_start, cmd_stop, cmd_restart
            cmd_map = {"start": cmd_start, "stop": cmd_stop, "restart": cmd_restart}
            return _print_result(cmd_map[args.command](alfred_home))

        return _print_result({"ok": False, "command": args.command,
                              "error": f"unknown command: {args.command}"})

    except Exception as exc:
        return _print_result({"ok": False, "command": args.command, "error": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
