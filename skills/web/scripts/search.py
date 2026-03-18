#!/usr/bin/env python3
"""Unified multi-backend web search CLI for agent workflows."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


class SearchError(RuntimeError):
    """Raised when a backend search operation fails."""


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
    extracted_text: str | None = None


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

    def to_dict(self) -> dict:
        """Return a JSON-serializable response."""
        return {
            "ok": self.ok,
            "query": self.query,
            "search_type": self.search_type,
            "backend": self.backend,
            "attempted_backends": self.attempted_backends,
            "count": self.count,
            "results": [asdict(item) for item in self.results],
            "errors": self.errors,
        }


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


def build_backend_order(selected: str) -> list[str]:
    """Resolve backend order for the requested mode."""
    if selected != "auto":
        return [selected]

    order = []
    if os.environ.get("TAVILY_API_KEY"):
        order.append("tavily")
    order.append("ddgs")
    return order


def search_with_fallback(args: argparse.Namespace, backend_map: dict[str, BaseBackend]) -> SearchResponse:
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
            if args.extract and results:
                extract_result_pages(results, top_n=args.extract_top, timeout=args.timeout)
            return SearchResponse(
                ok=True,
                query=args.query,
                search_type=args.type,
                backend=backend_name,
                attempted_backends=attempted,
                count=len(results),
                results=results,
                errors=errors,
            )
        except SearchError as exc:
            logger.warning("Search backend failed", extra={"backend": backend_name, "error": str(exc)})
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
    )


def extract_result_pages(results: list[SearchResult], top_n: int, timeout: int) -> None:
    """Fetch and extract readable text from top result pages."""
    requests = _load_requests()
    BeautifulSoup = _load_bs4()

    limit = max(0, min(top_n, len(results)))
    for item in results[:limit]:
        try:
            response = requests.get(
                item.url,
                headers={"User-Agent": "Mozilla/5.0 (everbot-web-search)"},
                timeout=timeout,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = " ".join(soup.get_text(" ", strip=True).split())
            item.extracted_text = text[:1200] if text else None
        except Exception as exc:  # pragma: no cover - network behavior
            item.extracted_text = None
            logger.warning("Page extraction failed", extra={"url": item.url, "error": str(exc)})


def format_text(response: SearchResponse) -> str:
    """Render a human-readable text report."""
    lines = [
        f"Query: {response.query}",
        f"Type: {response.search_type}",
        f"Backend: {response.backend or 'none'}",
        f"Results: {response.count}",
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
        if item.extracted_text:
            lines.append(f"Extracted: {item.extracted_text}")
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
