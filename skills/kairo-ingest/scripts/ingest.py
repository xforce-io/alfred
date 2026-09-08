#!/usr/bin/env python3
"""Unattended ingest: scan new XXX-YYMMDD items, auto-assign topic, add (no step).

Stdout is the user-facing message, or NO_USER_MESSAGE when there is nothing new.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from apply import build_actions, format_receipt, kairo_bin, run_actions
from scan import DEFAULT_VOICE_DIR, SILENT_TOKEN, assign_topics, new_import_items, scan


def ingest(
    *,
    root: Path,
    downloads: Path,
    recordings: Path,
    binary: str,
    dry_run: bool,
    step: bool,
) -> tuple[str, bool]:
    payload = scan(root, downloads, recordings, binary)
    items = assign_topics(new_import_items(payload.get("items") or []))
    if not items:
        return SILENT_TOKEN, False
    plan = {"root": str(root), "items": items}
    actions = build_actions(plan, step=step)
    result = run_actions(
        actions,
        binary=binary,
        dry_run=dry_run,
        root=None if dry_run else root,
    )
    return format_receipt(plan, result), bool(result.get("failed"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent-driven ingest of new XXX-YYMMDD materials")
    parser.add_argument("--root", default=str(Path.home() / "kairo"))
    parser.add_argument("--downloads", default=str(Path.home() / "Downloads"))
    parser.add_argument("--voice-memos", default="")
    parser.add_argument("--kairo-bin", default=kairo_bin())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--step", action="store_true", help="Also run kairo step (slow; ASR)")
    args = parser.parse_args(argv)
    recordings = Path(args.voice_memos).expanduser() if args.voice_memos else DEFAULT_VOICE_DIR
    try:
        text, failed = ingest(
            root=Path(args.root).expanduser(),
            downloads=Path(args.downloads).expanduser(),
            recordings=recordings,
            binary=args.kairo_bin,
            dry_run=args.dry_run,
            step=args.step,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(text + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
