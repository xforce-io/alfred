#!/usr/bin/env python3
"""CLI wrapper for routine CRUD operations."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _setup_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Routine manager CLI")
    parser.add_argument("--workspace", required=True, help="Agent workspace root")

    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List routines")
    list_p.add_argument("--include-disabled", action="store_true")

    add_p = sub.add_parser("add", help="Add routine")
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--description", default="")
    add_p.add_argument("--schedule", default=None)
    add_p.add_argument("--execution-mode", default="auto", choices=["auto", "inline", "isolated"])
    add_p.add_argument("--timezone", default=None)
    add_p.add_argument("--source", default="manual")
    add_p.add_argument("--timeout-seconds", type=int, default=120)
    add_p.add_argument("--allow-duplicate", action="store_true")
    add_p.add_argument("--next-run-at", default=None, help="ISO-8601 datetime for one-shot tasks")
    add_p.add_argument("--delay", default=None, help="Relative delay like '1m', '30s', '2h' (converted to --next-run-at)")

    upd_p = sub.add_parser("update", help="Update routine")
    upd_p.add_argument("--id", required=True, dest="task_id")
    upd_p.add_argument("--title", default=None)
    upd_p.add_argument("--description", default=None)
    upd_p.add_argument("--schedule", default=None)
    upd_p.add_argument("--execution-mode", default=None, choices=["auto", "inline", "isolated"])
    upd_p.add_argument("--timezone", default=None)
    upd_p.add_argument("--source", default=None)
    upd_p.add_argument("--enabled", default=None, choices=["true", "false"])
    upd_p.add_argument("--timeout-seconds", type=int, default=None)

    rm_p = sub.add_parser("remove", help="Remove routine")
    rm_p.add_argument("--id", required=True, dest="task_id")
    rm_p.add_argument("--hard", action="store_true")

    return parser


def _parse_delay(delay_str: str) -> str:
    """Parse a human-friendly delay string (e.g. '1m', '30s', '2h') into ISO-8601 datetime."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", delay_str.strip().lower())
    if not m:
        raise ValueError(f"Invalid delay format: {delay_str!r}. Use e.g. '30s', '1m', '2h', '1d'.")
    value, unit = int(m.group(1)), m.group(2)
    delta = {"s": timedelta(seconds=value), "m": timedelta(minutes=value),
             "h": timedelta(hours=value), "d": timedelta(days=value)}[unit]
    return (datetime.now(timezone.utc).astimezone() + delta).isoformat()


def _print_ok(payload: Any) -> int:
    print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False))
    return 0


def _print_error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    return 1


def main() -> int:
    _setup_import_path()
    parser = _build_parser()
    args = parser.parse_args()

    try:
        from src.everbot.core.tasks.routine_manager import RoutineManager
    except Exception as exc:
        return _print_error(f"import_failed: {exc}")

    manager = RoutineManager(Path(args.workspace).expanduser())
    try:
        if args.command == "list":
            routines = manager.list_routines(include_disabled=args.include_disabled)
            return _print_ok(routines)

        if args.command == "add":
            next_run_at = args.next_run_at
            if args.delay and not next_run_at:
                next_run_at = _parse_delay(args.delay)
            created = manager.add_routine(
                title=args.title,
                description=args.description,
                schedule=args.schedule,
                execution_mode=args.execution_mode,
                timezone_name=args.timezone,
                source=args.source,
                timeout_seconds=args.timeout_seconds,
                allow_duplicate=args.allow_duplicate,
                next_run_at=next_run_at,
            )
            return _print_ok(created)

        if args.command == "update":
            enabled = None
            if args.enabled is not None:
                enabled = args.enabled == "true"
            updated = manager.update_routine(
                args.task_id,
                title=args.title,
                description=args.description,
                schedule=args.schedule,
                execution_mode=args.execution_mode,
                timezone_name=args.timezone,
                source=args.source,
                enabled=enabled,
                timeout_seconds=args.timeout_seconds,
            )
            if updated is None:
                return _print_error("routine_not_found")
            return _print_ok(updated)

        if args.command == "remove":
            removed = manager.remove_routine(args.task_id, soft_disable=(not args.hard))
            if not removed:
                return _print_error("routine_not_found")
            return _print_ok({"removed": True, "soft_disable": (not args.hard)})

        return _print_error(f"unknown_command: {args.command}")
    except Exception as exc:
        return _print_error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
