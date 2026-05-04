#!/usr/bin/env python3
"""CLI wrapper for keyword recall over agent memory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _setup_import_path() -> None:
    """Add repo root to sys.path so ``src.everbot.*`` imports resolve."""
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keyword recall over agent memory (profile + events) via BM25-lite.",
    )
    parser.add_argument(
        "--workspace", required=True,
        help="Agent workspace root (contains MEMORY.md and events/)",
    )
    parser.add_argument(
        "--query", required=True,
        help="Search keywords or short phrase",
    )
    parser.add_argument(
        "--kind", default="both", choices=["profile", "event", "both"],
        help="Which memory layer to search (default: both)",
    )
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="Maximum results to return (default: 5)",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="For events, restrict to past N days (omit to search all)",
    )
    return parser


def _print_ok(payload: Any) -> int:
    print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False))
    return 0


def _print_error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    return 1


def main() -> int:
    _setup_import_path()
    args = _build_parser().parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists():
        return _print_error(f"workspace not found: {workspace}")

    memory_path = workspace / "MEMORY.md"
    events_dir = workspace / "events"

    try:
        from src.everbot.core.memory.manager import MemoryManager
    except Exception as exc:
        return _print_error(f"import_failed: {exc}")

    try:
        mm = MemoryManager(memory_path, events_dir=events_dir)
        results = mm.recall(
            query=args.query,
            kind=args.kind,
            top_k=args.top_k,
            days=args.days,
        )
    except ValueError as exc:
        return _print_error(str(exc))
    except Exception as exc:  # pragma: no cover — surface any unexpected error
        return _print_error(f"recall_failed: {exc}")

    return _print_ok(results)


if __name__ == "__main__":
    raise SystemExit(main())
