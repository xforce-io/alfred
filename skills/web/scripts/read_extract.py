#!/usr/bin/env python3
"""Paged reader for web extract cache (content_id + offset/limit only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as script: ensure scripts dir on path
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from extract_cache import (  # noqa: E402
    MAX_READ_LIMIT,
    CacheError,
    ExtractCache,
    InvalidContentIdError,
    InvalidRangeError,
    NotFoundError,
    format_content_id,
    parse_content_id,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a page of cached web extract text by content_id",
    )
    parser.add_argument(
        "--content-id",
        required=True,
        help="sha256 hex or sha256:<hex>",
    )
    parser.add_argument(
        "--offset",
        type=int,
        required=True,
        help="Unicode character offset (>= 0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        required=True,
        help=f"Max characters to return (1..{MAX_READ_LIMIT})",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    # Explicitly do not support --all or path args
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.offset < 0:
        print("invalid range: offset must be >= 0", file=sys.stderr)
        return 2
    if args.limit < 1 or args.limit > MAX_READ_LIMIT:
        print(
            f"invalid range: limit must be 1..{MAX_READ_LIMIT}",
            file=sys.stderr,
        )
        return 2

    try:
        hex_digest = parse_content_id(args.content_id)
    except InvalidContentIdError:
        print("invalid content_id", file=sys.stderr)
        return 2

    content_id = format_content_id(hex_digest)
    cache = ExtractCache()

    try:
        chars_full = cache.full_char_len(content_id)
        text = cache.read_range(content_id, args.offset, args.limit)
    except InvalidContentIdError:
        print("invalid content_id", file=sys.stderr)
        return 2
    except InvalidRangeError:
        print("invalid range", file=sys.stderr)
        return 2
    except NotFoundError:
        print("not found", file=sys.stderr)
        return 1
    except CacheError as exc:
        code = getattr(exc, "code", "cache_error")
        print(code, file=sys.stderr)
        return 1

    eof = args.offset + len(text) >= chars_full or args.offset >= chars_full

    if args.output == "json":
        payload = {
            "content_id": content_id,
            "offset": args.offset,
            "limit": args.limit,
            "chars_full": chars_full,
            "chars_returned": len(text),
            "eof": eof,
            "text": text,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        # Pure fragment for agent citation
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
