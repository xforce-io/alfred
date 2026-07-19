#!/usr/bin/env python3
"""Unified multi-backend web search CLI for agent workflows.

Default extract path stores full page text in a content-addressed cache and
returns a short structured material card (shared visible text budget).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Allow running as script
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from extract_cache import (  # noqa: E402
    CacheError,
    CacheFullError,
    CacheUnavailableError,
    ExtractCache,
    MAX_READ_LIMIT,
    format_content_id,
    normalize_text,
    parse_content_id,
)

logger = logging.getLogger(__name__)

DEFAULT_VISIBLE_CHARS = 4000
TITLE_MAX = 300
URL_MAX = 2000
META_MAX = 200
SCHEMA_VERSION = 2


class SearchError(RuntimeError):
    """Raised when a backend search operation fails."""


@dataclass
class ExtractInfo:
    """Successful extract payload for one result."""

    preview: str
    preview_truncated: bool
    chars_full: int
    content_id: str
    read_command: str


@dataclass
class SearchResult:
    """Normalized search result entry."""

    title: str
    url: str
    snippet: str
    source: str | None
    published: str | None
    backend: str
    search_type: str
    rank: int
    extracted_text: str | None = None  # deprecated alias of extract.preview
    extract: ExtractInfo | None = None
    extract_error: str | None = None
    snippet_truncated: bool = False
    # Internal: full text held until budget allocation (not serialized raw)
    _full_text: str | None = field(default=None, repr=False, compare=False)


@dataclass
class SearchResponse:
    """Normalized search response."""

    ok: bool
    query: str
    search_type: str
    backend: str | None
    attempted_backends: list[str]
    count: int
    results: list[SearchResult]
    errors: list[dict]
    materials_hint: str = "no_extract"
    stats: dict[str, int] | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Return a JSON-serializable response (schema v2)."""
        results_out: list[dict[str, Any]] = []
        for item in self.results:
            entry: dict[str, Any] = {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "snippet_truncated": item.snippet_truncated,
                "source": item.source,
                "published": item.published,
                "backend": item.backend,
                "search_type": item.search_type,
                "rank": item.rank,
                "extract": None,
                "extracted_text": item.extracted_text,
                "extract_error": item.extract_error,
            }
            if item.extract is not None:
                entry["extract"] = {
                    "preview": item.extract.preview,
                    "preview_truncated": item.extract.preview_truncated,
                    "chars_full": item.extract.chars_full,
                    "content_id": item.extract.content_id,
                    "read_command": item.extract.read_command,
                }
            results_out.append(entry)

        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "query": self.query,
            "search_type": self.search_type,
            "backend": self.backend,
            "attempted_backends": self.attempted_backends,
            "count": self.count,
            "result_count": self.count,
            "materials_hint": self.materials_hint,
            "results": results_out,
            "errors": self.errors,
        }
        if self.stats is not None:
            payload["stats"] = self.stats
        return payload


class BaseBackend:
    """Search backend interface."""

    name = "base"

    def is_available(self) -> bool:
        """Return True when the backend can be used."""
        return True

    def search(self, args: argparse.Namespace) -> list[SearchResult]:
        """Execute a search query."""
        raise NotImplementedError


class DDGSBackend(BaseBackend):
    """DDGS-based backend."""

    name = "ddgs"

    def is_available(self) -> bool:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return False
        return True

    def search(self, args: argparse.Namespace) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise SearchError("ddgs package is not installed") from exc

        try:
            client = DDGS(timeout=args.timeout, verify=True)
            if args.type == "news":
                raw_results = client.news(
                    args.query,
                    max_results=args.max_results,
                )
            else:
                raw_results = client.text(
                    args.query,
                    region=args.region,
                    safesearch=args.safesearch,
                    timelimit=args.timelimit,
                    max_results=args.max_results,
                )
        except Exception as exc:  # pragma: no cover - dependency runtime behavior
            raise SearchError(f"ddgs search failed: {exc}") from exc

        return normalize_ddgs_results(raw_results, search_type=args.type)


class TavilyBackend(BaseBackend):
    """Tavily search backend."""

    name = "tavily"
    url = "https://api.tavily.com/search"

    def is_available(self) -> bool:
        return bool(os.environ.get("TAVILY_API_KEY"))

    def search(self, args: argparse.Namespace) -> list[SearchResult]:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise SearchError("TAVILY_API_KEY is not set")

        requests = _load_requests()
        payload = {
            "api_key": api_key,
            "query": args.query,
            "topic": "news" if args.type == "news" else "general",
            "search_depth": "basic",
            "max_results": args.max_results,
            "include_answer": False,
            "include_images": False,
            "include_raw_content": False,
        }
        try:
            response = requests.post(
                self.url,
                json=payload,
                timeout=args.timeout,
            )
            response.raise_for_status()
            raw_payload = response.json()
        except Exception as exc:  # pragma: no cover - network behavior
            raise SearchError(f"tavily search failed: {exc}") from exc

        return normalize_tavily_results(raw_payload, search_type=args.type)


def _load_requests():
    try:
        import requests
    except ImportError as exc:
        raise SearchError("requests package is not installed") from exc
    return requests


def _load_bs4():
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SearchError("beautifulsoup4 package is not installed") from exc
    return BeautifulSoup


def normalize_ddgs_results(raw_results: Iterable[dict], search_type: str) -> list[SearchResult]:
    """Normalize DDGS result objects."""
    results: list[SearchResult] = []
    for index, item in enumerate(raw_results, start=1):
        url = item.get("href") or item.get("url") or ""
        results.append(
            SearchResult(
                title=item.get("title", ""),
                url=url,
                snippet=item.get("body", ""),
                source=item.get("source"),
                published=item.get("date"),
                backend="ddgs",
                search_type=search_type,
                rank=index,
            )
        )
    return dedupe_results(results)


def normalize_tavily_results(payload: dict, search_type: str) -> list[SearchResult]:
    """Normalize Tavily payload."""
    items = payload.get("results", [])
    results: list[SearchResult] = []
    for index, item in enumerate(items, start=1):
        results.append(
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                source=item.get("source"),
                published=item.get("published_date"),
                backend="tavily",
                search_type=search_type,
                rank=index,
            )
        )
    return dedupe_results(results)


def dedupe_results(results: Iterable[SearchResult]) -> list[SearchResult]:
    """Deduplicate results by normalized URL while preserving order."""
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for item in results:
        key = normalize_url(item.url)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    for index, item in enumerate(deduped, start=1):
        item.rank = index
    return deduped


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication."""
    return url.strip().rstrip("/").lower()


def truncate_field(value: str | None, max_len: int) -> str | None:
    """Truncate a short metadata field to a conservative max length."""
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[:max_len]


def clamp_short_fields(results: list[SearchResult]) -> None:
    """Apply independent caps on title/url/source/published."""
    for item in results:
        item.title = truncate_field(item.title or "", TITLE_MAX) or ""
        item.url = truncate_field(item.url or "", URL_MAX) or ""
        item.source = truncate_field(item.source, META_MAX)
        item.published = truncate_field(item.published, META_MAX)


def resolve_visible_budget(args: argparse.Namespace | None = None) -> int:
    """CLI --visible-chars > env > default 4000."""
    if args is not None:
        cli_val = getattr(args, "visible_chars", None)
        if cli_val is not None:
            return max(0, int(cli_val))
    env = os.environ.get("ALFRED_WEB_EXTRACT_VISIBLE_CHARS")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return DEFAULT_VISIBLE_CHARS


def make_read_command(content_id: str) -> str:
    """Build portable read_command using $SKILL_DIR (no absolute paths)."""
    hex_digest = parse_content_id(content_id)
    return (
        f"python $SKILL_DIR/scripts/read_extract.py "
        f"--content-id {hex_digest} --offset 0 --limit {MAX_READ_LIMIT}"
    )


def allocate_visible_text(
    results: list[SearchResult],
    budget: int,
    *,
    full_extract: bool = False,
) -> dict[str, int]:
    """Allocate shared snippet+preview budget by rank ascending.

    Mutates results in place. Returns stats dict with visible_text_chars,
    visible_text_budget, and full_chars_total.
    """
    remaining = max(0, budget)
    full_chars_total = 0
    visible = 0

    for item in sorted(results, key=lambda r: r.rank):
        snip_src = item.snippet or ""
        if len(snip_src) <= remaining:
            item.snippet = snip_src
            item.snippet_truncated = False
            remaining -= len(item.snippet)
            visible += len(item.snippet)
        else:
            item.snippet = snip_src[:remaining]
            item.snippet_truncated = True
            visible += len(item.snippet)
            remaining = 0

        if item.extract is None and item._full_text is None:
            continue

        full_body = item._full_text
        if full_body is None and item.extract is not None:
            preview = item.extract.preview
            full_chars_total += item.extract.chars_full
            visible += len(preview)
            item.extracted_text = preview
            continue

        assert full_body is not None
        chars_full = len(full_body)
        full_chars_total += chars_full

        if full_extract:
            preview = full_body
            truncated = False
        elif remaining <= 0:
            preview = ""
            truncated = chars_full > 0
        elif len(full_body) <= remaining:
            preview = full_body
            truncated = False
            remaining -= len(preview)
        else:
            preview = full_body[:remaining]
            truncated = True
            remaining = 0

        visible += len(preview)

        content_id = item.extract.content_id if item.extract is not None else ""
        read_command = (
            item.extract.read_command
            if item.extract is not None
            else (make_read_command(content_id) if content_id else "")
        )
        item.extract = ExtractInfo(
            preview=preview,
            preview_truncated=truncated,
            chars_full=chars_full,
            content_id=content_id,
            read_command=read_command,
        )
        item.extracted_text = preview

    return {
        "visible_text_chars": visible,
        "visible_text_budget": budget,
        "full_chars_total": full_chars_total,
    }


def build_backend_order(selected: str) -> list[str]:
    """Resolve backend order for the requested mode."""
    if selected != "auto":
        return [selected]

    order = []
    if os.environ.get("TAVILY_API_KEY"):
        order.append("tavily")
    order.append("ddgs")
    return order


def search_with_fallback(
    args: argparse.Namespace,
    backend_map: dict[str, BaseBackend],
    *,
    cache: ExtractCache | None = None,
) -> SearchResponse:
    """Try backends in order until one succeeds."""
    attempted: list[str] = []
    errors: list[dict] = []
    order = build_backend_order(args.backend)

    if not args.fallback and order:
        order = order[:1]

    for backend_name in order:
        backend = backend_map[backend_name]
        attempted.append(backend_name)

        if not backend.is_available():
            errors.append({"backend": backend_name, "error": "backend unavailable"})
            continue

        try:
            results = backend.search(args)
            clamp_short_fields(results)
            budget = resolve_visible_budget(args)
            full_extract = bool(getattr(args, "full_extract", False))

            if args.extract and results:
                extract_result_pages(
                    results,
                    top_n=args.extract_top,
                    timeout=args.timeout,
                    cache=cache,
                )

            stats = allocate_visible_text(
                results,
                budget,
                full_extract=full_extract and bool(args.extract),
            )
            materials_hint = (
                "extract_available"
                if any(r.extract is not None for r in results)
                else "no_extract"
            )

            return SearchResponse(
                ok=True,
                query=args.query,
                search_type=args.type,
                backend=backend_name,
                attempted_backends=attempted,
                count=len(results),
                results=results,
                errors=errors,
                materials_hint=materials_hint,
                stats=stats,
            )
        except SearchError as exc:
            logger.warning(
                "Search backend failed",
                extra={"backend": backend_name, "error": str(exc)},
            )
            errors.append({"backend": backend_name, "error": str(exc)})

    return SearchResponse(
        ok=False,
        query=args.query,
        search_type=args.type,
        backend=None,
        attempted_backends=attempted,
        count=0,
        results=[],
        errors=errors,
        materials_hint="no_extract",
        stats={
            "visible_text_chars": 0,
            "visible_text_budget": resolve_visible_budget(args),
            "full_chars_total": 0,
        },
    )


def extract_result_pages(
    results: list[SearchResult],
    top_n: int,
    timeout: int,
    cache: ExtractCache | None = None,
    *,
    html_fetcher=None,
) -> None:
    """Fetch and extract readable text from top result pages; store full text.

    On success sets item.extract skeleton (preview filled later by budget) and
    item._full_text. On failure sets extract=None and extract_error.
    """
    cache = cache or ExtractCache()
    limit = max(0, min(top_n, len(results)))

    for item in results[:limit]:
        try:
            if html_fetcher is not None:
                html = html_fetcher(item.url)
            else:
                requests = _load_requests()
                response = requests.get(
                    item.url,
                    headers={"User-Agent": "Mozilla/5.0 (everbot-web-search)"},
                    timeout=timeout,
                )
                response.raise_for_status()
                html = response.text

            BeautifulSoup = _load_bs4()
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = normalize_text(soup.get_text(" ", strip=True))
            if not text:
                item.extract = None
                item.extracted_text = None
                item.extract_error = "empty_body"
                item._full_text = None
                continue

            try:
                content_id = cache.store(text)
            except CacheFullError:
                item.extract = None
                item.extracted_text = None
                item.extract_error = "cache_full"
                item._full_text = None
                continue
            except CacheUnavailableError:
                item.extract = None
                item.extracted_text = None
                item.extract_error = "cache_unavailable"
                item._full_text = None
                continue
            except CacheError:
                item.extract = None
                item.extracted_text = None
                item.extract_error = "cache_error"
                item._full_text = None
                continue

            # Stored text may re-normalize identically
            stored = normalize_text(text)
            item._full_text = stored
            item.extract = ExtractInfo(
                preview="",  # filled by allocate_visible_text
                preview_truncated=True,
                chars_full=len(stored),
                content_id=content_id,
                read_command=make_read_command(content_id),
            )
            item.extracted_text = None  # set after budget
            item.extract_error = None
        except Exception as exc:  # pragma: no cover - network behavior
            item.extract = None
            item.extracted_text = None
            item.extract_error = "extract_failed"
            item._full_text = None
            logger.warning(
                "Page extraction failed",
                extra={"url": item.url, "error": str(exc)},
            )


def format_text(response: SearchResponse) -> str:
    """Render a human-readable text report (material card style)."""
    lines = [
        f"Query: {response.query}",
        f"Type: {response.search_type}",
        f"Backend: {response.backend or 'none'}",
        f"Results: {response.count}",
        f"materials_hint: {response.materials_hint}",
    ]
    if response.errors:
        lines.append("Errors:")
        for err in response.errors:
            lines.append(f"- {err['backend']}: {err['error']}")

    for item in response.results:
        lines.append("")
        lines.append(f"[{item.rank}] {item.title}")
        lines.append(f"URL: {item.url}")
        if item.source:
            lines.append(f"Source: {item.source}")
        if item.published:
            lines.append(f"Published: {item.published}")
        if item.snippet:
            lines.append(f"Snippet: {item.snippet}")
        if item.extract is not None:
            lines.append(f"Extracted: {item.extract.preview}")
            lines.append(f"content_id: {item.extract.content_id}")
            lines.append(f"Read: {item.extract.read_command}")
            lines.append(f"chars_full: {item.extract.chars_full}")
        elif item.extract_error:
            lines.append(f"extract_error: {item.extract_error}")
        elif item.extracted_text:
            lines.append(f"Extracted: {item.extracted_text}")

    if response.stats:
        lines.append("")
        lines.append(
            "Stats: "
            f"visible_text_chars={response.stats.get('visible_text_chars', 0)} "
            f"full_chars_total={response.stats.get('full_chars_total', 0)}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Unified web search skill")
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--backend",
        choices=["auto", "ddgs", "tavily"],
        default="auto",
        help="Search backend selection",
    )
    parser.add_argument(
        "--type",
        choices=["text", "news"],
        default="text",
        help="Search type",
    )
    parser.add_argument("--max-results", type=int, default=5, help="Maximum result count")
    parser.add_argument("--region", default="wt-wt", help="Region hint, such as wt-wt or cn-zh")
    parser.add_argument("--timelimit", default=None, help="Time limit: d/w/m/y")
    parser.add_argument(
        "--safesearch",
        choices=["on", "moderate", "off"],
        default="moderate",
        help="Safe search mode",
    )
    parser.add_argument("--output", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")
    parser.add_argument("--extract", action="store_true", help="Extract readable page text")
    parser.add_argument("--extract-top", type=int, default=2, help="Number of top results to extract")
    parser.add_argument(
        "--visible-chars",
        type=int,
        default=None,
        help=f"Shared snippet+preview budget (default {DEFAULT_VISIBLE_CHARS})",
    )
    parser.add_argument(
        "--full-extract",
        action="store_true",
        help="DEBUG only: include full extract text in stdout (not for agent cite path)",
    )
    parser.add_argument(
        "--no-fallback",
        dest="fallback",
        action="store_false",
        help="Disable backend fallback",
    )
    parser.set_defaults(fallback=True)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    backend_map: dict[str, BaseBackend] = {
        "ddgs": DDGSBackend(),
        "tavily": TavilyBackend(),
    }
    response = search_with_fallback(args, backend_map)

    if args.output == "json":
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_text(response))
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
