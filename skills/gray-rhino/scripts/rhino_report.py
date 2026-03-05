#!/usr/bin/env python3
"""Gray Rhino comprehensive report generator.

Orchestrates: news_fetcher -> rhino_analyzer -> asset_mapper -> report
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone

# Add scripts dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from news_fetcher import NewsFetcher, _check_dependencies
from rhino_analyzer import RhinoAnalyzer, CATEGORY_NAMES_ZH
from asset_mapper import AssetMapper, ASSET_CLASSES

logger = logging.getLogger(__name__)


def generate_report(max_age_hours: int = 48, top_n: int = 5,
                    category_filter: str = None,
                    min_cluster_size: int = 2) -> dict:
    """Generate a full gray rhino report.

    Returns:
        Dict with report data including clusters, impacts, and metadata.
    """
    # Step 1: Fetch news
    fetcher = NewsFetcher()
    items = fetcher.fetch_all(max_age_hours=max_age_hours)
    logger.info(f"Fetched {len(items)} news items")

    if not items:
        return {
            "ok": False,
            "error": "No news items fetched. Check network connectivity.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # Step 2: Cluster and analyze
    analyzer = RhinoAnalyzer(min_cluster_size=min_cluster_size)
    items_dicts = [asdict(item) for item in items]
    # Remove internal fields
    for d in items_dicts:
        d.pop("_hash", None)
    clusters = analyzer.analyze(items_dicts)

    # Filter by category if specified
    if category_filter:
        clusters = [c for c in clusters
                    if c["category"] == category_filter
                    or c["category_zh"] == category_filter]

    # Keep top N
    clusters = clusters[:top_n]

    # Step 3: Map to asset impacts
    mapper = AssetMapper()
    matrix = mapper.map_clusters(clusters)

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "news_count": len(items),
        "cluster_count": len(clusters),
        "clusters": clusters,
        "impact_matrix": asdict(matrix),
        "assets": list(ASSET_CLASSES.keys()),
    }


def format_text_report(report: dict) -> str:
    """Format report as human-readable text."""
    if not report.get("ok"):
        return f"Error: {report.get('error', 'Unknown error')}"

    lines = []
    gen_time = report.get("generated_at", "")[:19].replace("T", " ")

    lines.append("=" * 60)
    lines.append(f"  🦏 灰犀牛风险预警报告 | {gen_time}")
    lines.append("=" * 60)
    lines.append(f"  新闻来源: {report['news_count']} 条 | "
                 f"识别聚类: {report['cluster_count']} 个")
    lines.append("")

    clusters = report.get("clusters", [])
    events = report.get("impact_matrix", {}).get("events", [])

    # Build event lookup by matching scenario
    event_map = {}
    for evt in events:
        event_map[evt.get("event_name", "")] = evt

    if not clusters:
        lines.append("  当前未发现显著灰犀牛风险聚类。")
        lines.append("")
        return "\n".join(lines)

    category_icons = {
        "geopolitics": "🌍", "macro": "💰", "trade": "🚢",
        "tech_regulation": "🤖", "energy_climate": "⚡",
        "health_social": "🏥", "other": "📰",
    }

    for i, cluster in enumerate(clusters, 1):
        cat = cluster.get("category", "other")
        cat_zh = cluster.get("category_zh", cat)
        icon = category_icons.get(cat, "📰")

        # Severity color based on cluster size
        count = cluster.get("count", 0)
        if count >= 5:
            severity = "🔴"
        elif count >= 3:
            severity = "🟠"
        else:
            severity = "🟡"

        lines.append(f"{severity} [{cat_zh}] {cluster['representative_title']}")
        lines.append(f"   相关报道: {count} 条 | "
                     f"来源: {', '.join(cluster.get('sources', []))}")
        lines.append(f"   关键词: {', '.join(cluster.get('keywords', [])[:6])}")

        # Show related titles
        titles = cluster.get("titles", [])
        if len(titles) > 1:
            for t in titles[1:3]:
                lines.append(f"   · {t}")
            if len(titles) > 3:
                lines.append(f"   · ...及 {len(titles)-3} 条更多")

        # Show asset impact if matched
        event_name = cluster["representative_title"]
        event = event_map.get(event_name)
        if event:
            impacts = event.get("impacts", [])
            if impacts:
                lines.append(f"   匹配场景: {event.get('matched_scenario', 'N/A')}")
                lines.append(f"   ┌{'─' * 11}┬{'─' * 8}┐")
                lines.append(f"   │ {'资产':<8} │ {'影响':<5} │")
                lines.append(f"   ├{'─' * 11}┼{'─' * 8}┤")
                for imp in impacts:
                    asset = imp.get("asset", "")
                    direction = imp.get("direction", "±")
                    lines.append(f"   │ {asset:<8} │ {direction:<5} │")
                lines.append(f"   └{'─' * 11}┴{'─' * 8}┘")
        else:
            lines.append("   💡 提示: 此聚类未匹配到已知风险场景，建议人工评估")

        lines.append("")

    # Summary
    lines.append("-" * 60)
    lines.append("📋 汇总")
    matched_count = len(events)
    lines.append(f"  已匹配场景: {matched_count}/{len(clusters)} 个聚类")
    if events:
        # Find most impacted assets (deduplicate scenarios)
        asset_impacts = {}
        for evt in events:
            for imp in evt.get("impacts", []):
                asset = imp["asset"]
                direction = imp["direction"]
                scenario = evt.get("matched_scenario", "")
                if direction in ("↑↑", "↓↓"):
                    key = f"{direction}({scenario})"
                    asset_impacts.setdefault(asset, set()).add(key)
        if asset_impacts:
            lines.append("  强影响资产:")
            for asset, impacts_list in asset_impacts.items():
                lines.append(f"    {asset}: {' / '.join(sorted(impacts_list))}")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate gray rhino risk report",
        epilog="Examples:\n"
               "  python rhino_report.py --format text\n"
               "  python rhino_report.py --max-age 24 --top 3 --format json\n"
               "  python rhino_report.py --fetch-only\n"
               "  python rhino_report.py --category geopolitics\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--max-age", type=int, default=48,
                        help="News age window in hours (default: 48)")
    parser.add_argument("--top", type=int, default=5,
                        help="Top N risk clusters to report (default: 5)")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter by risk category (e.g., geopolitics, macro)")
    parser.add_argument("--min-cluster", type=int, default=2,
                        help="Minimum cluster size (default: 2)")
    parser.add_argument("--format", choices=["json", "text"], default="text",
                        help="Output format (default: text)")
    parser.add_argument("--fetch-only", action="store_true",
                        help="Only fetch news, skip analysis (debug mode)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    _check_dependencies()

    if args.fetch_only:
        fetcher = NewsFetcher()
        items = fetcher.fetch_all(max_age_hours=args.max_age)
        output = [asdict(item) for item in items]
        for o in output:
            o.pop("_hash", None)
        if args.format == "json":
            print(json.dumps({"ok": True, "count": len(output), "items": output},
                             ensure_ascii=False, indent=2))
        else:
            print(f"Fetched {len(items)} news items\n")
            for i, item in enumerate(items, 1):
                print(f"[{i}] {item.title}")
                print(f"    Source: {item.source} | {item.published or 'N/A'}")
                if item.summary:
                    print(f"    {item.summary[:120]}")
                print()
        return

    report = generate_report(
        max_age_hours=args.max_age,
        top_n=args.top,
        category_filter=args.category,
        min_cluster_size=args.min_cluster,
    )

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report))


if __name__ == "__main__":
    main()
