#!/usr/bin/env python3
"""RSS/Web news fetcher for gray rhino risk analysis."""

import argparse
import hashlib
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default RSS sources
# ---------------------------------------------------------------------------
DEFAULT_RSS_SOURCES = {
    # International
    "NPR World": "https://feeds.npr.org/1004/rss.xml",
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    # Finance
    "CNBC Top": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC World": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    # Energy / Commodities
    "OilPrice": "https://oilprice.com/rss/main",
    # Chinese (fallback - may need proxy)
    "Sina Finance": "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=50&page=1&r=0.1&callback=",
    "Wallstreetcn": "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=30",
}

# Sources that use JSON API instead of RSS
JSON_API_SOURCES = {"Sina Finance", "Wallstreetcn"}


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    url: str
    published: Optional[str] = None  # ISO format string
    category: str = ""
    _hash: str = field(default="", repr=False)

    def __post_init__(self):
        if not self._hash:
            self._hash = hashlib.md5(self.title.encode()).hexdigest()[:12]


def _check_dependencies():
    missing = []
    try:
        import feedparser  # noqa: F401
    except ImportError:
        missing.append("feedparser")
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append("requests")
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}", file=sys.stderr)
        print(f"Install with: pip install {' '.join(missing)}", file=sys.stderr)
        sys.exit(1)


class NewsFetcher:
    """Fetches news from RSS feeds and JSON APIs."""

    def __init__(self, sources: Optional[dict] = None, timeout: int = 15):
        self.sources = sources or DEFAULT_RSS_SOURCES
        self.timeout = timeout

    def fetch_all(self, max_age_hours: int = 48) -> List[NewsItem]:
        """Fetch from all sources in parallel, deduplicate, and filter by age."""
        all_items: List[NewsItem] = []

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for name, url in self.sources.items():
                if name in JSON_API_SOURCES:
                    futures[pool.submit(self._fetch_json_api, name, url)] = name
                else:
                    futures[pool.submit(self._fetch_rss, name, url)] = name

            for future in as_completed(futures):
                source_name = futures[future]
                try:
                    items = future.result()
                    all_items.extend(items)
                    logger.info(f"[{source_name}] fetched {len(items)} items")
                except Exception as e:
                    logger.warning(f"[{source_name}] fetch failed: {e}")

        # Filter by age
        if max_age_hours > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
            filtered = []
            for item in all_items:
                if item.published:
                    try:
                        pub_ts = datetime.fromisoformat(item.published).timestamp()
                        if pub_ts >= cutoff:
                            filtered.append(item)
                            continue
                    except (ValueError, TypeError):
                        pass
                # Keep items with no valid date (might be recent)
                filtered.append(item)
            all_items = filtered

        all_items = self.deduplicate(all_items)
        all_items.sort(key=lambda x: x.published or "", reverse=True)
        return all_items

    def _fetch_rss(self, source_name: str, url: str) -> List[NewsItem]:
        """Parse a single RSS feed."""
        import feedparser
        import requests

        resp = requests.get(url, timeout=self.timeout, headers={
            "User-Agent": "Mozilla/5.0 (gray-rhino-bot)"
        })
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)

        items = []
        for entry in feed.entries:
            published = None
            if hasattr(entry, "published"):
                try:
                    dt = parsedate_to_datetime(entry.published)
                    published = dt.isoformat()
                except Exception:
                    published = entry.published
            elif hasattr(entry, "updated"):
                published = entry.updated

            summary = ""
            if hasattr(entry, "summary"):
                summary = _strip_html(entry.summary)[:500]
            elif hasattr(entry, "description"):
                summary = _strip_html(entry.description)[:500]

            items.append(NewsItem(
                title=entry.get("title", "").strip(),
                summary=summary,
                source=source_name,
                url=entry.get("link", ""),
                published=published,
            ))
        return items

    def _fetch_json_api(self, source_name: str, url: str) -> List[NewsItem]:
        """Fetch from JSON API sources (Sina, Wallstreetcn)."""
        import requests

        resp = requests.get(url, timeout=self.timeout, headers={
            "User-Agent": "Mozilla/5.0 (gray-rhino-bot)"
        })
        resp.raise_for_status()

        items = []
        if source_name == "Wallstreetcn":
            data = resp.json()
            for item in data.get("data", {}).get("items", []):
                dt = datetime.fromtimestamp(
                    item.get("display_time", 0), tz=timezone.utc
                )
                items.append(NewsItem(
                    title=item.get("title") or item.get("content_text", "")[:100],
                    summary=_strip_html(item.get("content_text", ""))[:500],
                    source=source_name,
                    url=f"https://wallstreetcn.com/live/{item.get('id', '')}",
                    published=dt.isoformat(),
                ))
        elif source_name == "Sina Finance":
            # Sina returns JSONP, strip callback
            text = resp.text
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                for item in data.get("result", {}).get("data", []):
                    items.append(NewsItem(
                        title=item.get("title", ""),
                        summary=item.get("summary", "")[:500],
                        source=source_name,
                        url=item.get("url", ""),
                        published=item.get("ctime", ""),
                    ))
        return items

    def deduplicate(self, items: List[NewsItem]) -> List[NewsItem]:
        """Remove duplicates by title similarity."""
        seen_hashes = set()
        seen_titles = []
        unique = []
        for item in items:
            if item._hash in seen_hashes:
                continue
            # Check title similarity (simple normalized match)
            norm_title = _normalize_title(item.title)
            if len(norm_title) < 5:
                continue
            is_dup = False
            for st in seen_titles:
                if _title_similarity(norm_title, st) > 0.75:
                    is_dup = True
                    break
            if not is_dup:
                seen_hashes.add(item._hash)
                seen_titles.append(norm_title)
                unique.append(item)
        return unique


def _strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r'<[^>]+>', '', text).strip()


def _normalize_title(title: str) -> str:
    """Normalize title for comparison."""
    return re.sub(r'[^\w\s]', '', title.lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity between two titles."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / min(len(words_a), len(words_b))


def main():
    parser = argparse.ArgumentParser(
        description="Fetch news from RSS feeds and APIs for gray rhino analysis"
    )
    parser.add_argument("--max-age", type=int, default=48,
                        help="Max news age in hours (default: 48)")
    parser.add_argument("--format", choices=["json", "text"], default="text",
                        help="Output format (default: text)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max number of items to return (0=unlimited)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)

    fetcher = NewsFetcher()
    items = fetcher.fetch_all(max_age_hours=args.max_age)

    if args.limit > 0:
        items = items[:args.limit]

    if args.format == "json":
        output = [asdict(item) for item in items]
        # Remove internal hash field
        for o in output:
            o.pop("_hash", None)
        print(json.dumps({"ok": True, "count": len(output), "items": output},
                         ensure_ascii=False, indent=2))
    else:
        print(f"Fetched {len(items)} news items\n")
        for i, item in enumerate(items, 1):
            print(f"[{i}] {item.title}")
            print(f"    Source: {item.source} | {item.published or 'N/A'}")
            if item.summary:
                print(f"    {item.summary[:120]}...")
            print()


if __name__ == "__main__":
    _check_dependencies()
    main()
