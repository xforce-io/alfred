#!/usr/bin/env python3
"""Gray Rhino comprehensive report generator.

Orchestrates: news_fetcher -> rhino_analyzer -> trend_tracker -> asset_mapper -> report

The key shift: instead of ranking by absolute coverage (already priced in),
we rank by trend signal strength — prioritizing emerging and accelerating risks.
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
from rhino_analyzer import RhinoAnalyzer
from asset_mapper import AssetMapper, ASSET_CLASSES
from trend_tracker import TrendTracker

logger = logging.getLogger(__name__)


def generate_report(max_age_hours: int = 48, top_n: int = 8,
                    category_filter: str = None,
                    min_cluster_size: int = 1,
                    history_dir: str = None,
                    lookback_days: int = 7,
                    save_snapshot: bool = True) -> dict:
    """Generate a trend-aware gray rhino report.

    Returns:
        Dict with report data including trend signals, impacts, and metadata.
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

    # Step 2: Cluster and analyze (min_cluster_size=1 to keep weak signals)
    analyzer = RhinoAnalyzer(min_cluster_size=min_cluster_size)
    items_dicts = [asdict(item) for item in items]
    for d in items_dicts:
        d.pop("_hash", None)
    clusters = analyzer.analyze(items_dicts)

    # Filter by category if specified
    if category_filter:
        clusters = [c for c in clusters
                    if c["category"] == category_filter
                    or c["category_zh"] == category_filter]

    # Step 3: Trend analysis — compare against historical baseline
    tracker_kwargs = {"lookback_days": lookback_days}
    if history_dir:
        tracker_kwargs["history_dir"] = history_dir
    tracker = TrendTracker(**tracker_kwargs)
    signals = tracker.analyze_trends(clusters)

    # Save today's snapshot for future trend comparison
    if save_snapshot:
        tracker.save_snapshot(clusters)

    # Step 4: Take top N by trend score
    signals = signals[:top_n]

    # Step 5: Map to asset impacts (only for clusters with enough context)
    mapper = AssetMapper()
    signal_clusters = []
    for sig in signals:
        # Rebuild minimal cluster dict for asset mapper
        signal_clusters.append({
            "representative_title": sig.representative_title,
            "category": sig.category,
            "category_zh": sig.category_zh,
            "keywords": sig.keywords,
            "titles": [sig.representative_title],
        })
    matrix = mapper.map_clusters(signal_clusters)

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "news_count": len(items),
        "total_clusters": len(clusters),
        "reported_signals": len(signals),
        "signals": [asdict(s) for s in signals],
        "impact_matrix": asdict(matrix),
        "assets": list(ASSET_CLASSES.keys()),
        "history_days": len(tracker.load_history()),
    }


def format_text_report(report: dict) -> str:
    """Format report as human-readable text with trend focus."""
    if not report.get("ok"):
        return f"Error: {report.get('error', 'Unknown error')}"

    lines = []
    gen_time = report.get("generated_at", "")[:19].replace("T", " ")
    history_days = report.get("history_days", 0)

    lines.append("=" * 62)
    lines.append(f"  灰犀牛趋势预警报告 | {gen_time}")
    lines.append("=" * 62)
    lines.append(f"  新闻源: {report['news_count']} 条 | "
                 f"聚类: {report['total_clusters']} 个 | "
                 f"历史基线: {history_days} 天")
    if history_days == 0:
        lines.append("  (首次运行，尚无历史基线。持续运行后趋势分析将更准确)")
    lines.append("")

    signals = report.get("signals", [])
    events = report.get("impact_matrix", {}).get("events", [])

    event_map = {}
    for evt in events:
        event_map[evt.get("event_name", "")] = evt

    if not signals:
        lines.append("  当前未发现显著趋势信号。")
        lines.append("")
        return "\n".join(lines)

    category_icons = {
        "geopolitics": "🌍", "macro": "💰", "trade": "🚢",
        "tech_regulation": "🤖", "energy_climate": "⚡",
        "health_social": "🏥", "other": "📰",
    }

    signal_icons = {
        "emerging": "🆕",
        "accelerating": "📈",
        "steady": "📊",
        "fading": "📉",
    }

    for i, sig in enumerate(signals, 1):
        cat = sig.get("category", "other")
        cat_zh = sig.get("category_zh", cat)
        cat_icon = category_icons.get(cat, "📰")
        sig_icon = signal_icons.get(sig.get("signal_type", ""), "❓")
        sig_zh = sig.get("signal_type_zh", "")

        lines.append(f"{sig_icon} {cat_icon} [{cat_zh}] {sig['representative_title']}")
        lines.append(f"   信号: {sig_zh} | 趋势分: {sig['trend_score']:.1f} | "
                     f"报道: {sig['current_count']} 条 | "
                     f"来源: {', '.join(sig.get('sources', []))}")

        # Trend details
        details = []
        if sig.get("novelty_score", 0) >= 0.8:
            details.append("新兴话题")
        if sig.get("acceleration", 1) > 1.5:
            details.append(f"加速×{sig['acceleration']:.1f}")
        if sig.get("source_diversity", 0) >= 0.75:
            details.append("多源印证")
        if sig.get("days_tracked", 0) > 0:
            history_counts = sig.get("history_counts", [])
            if history_counts:
                details.append(f"近{len(history_counts)}日: {history_counts}")
        if details:
            lines.append(f"   趋势: {' | '.join(details)}")

        lines.append(f"   关键词: {', '.join(sig.get('keywords', [])[:6])}")

        # Asset impact
        event = event_map.get(sig["representative_title"])
        if event:
            impacts = event.get("impacts", [])
            if impacts:
                lines.append(f"   匹配场景: {event.get('matched_scenario', 'N/A')}")
                impact_parts = []
                for imp in impacts:
                    d = imp.get("direction", "±")
                    if d != "±":
                        impact_parts.append(f"{imp['asset']}{d}")
                if impact_parts:
                    lines.append(f"   资产影响: {' | '.join(impact_parts)}")

        lines.append("")

    # Summary by signal type
    lines.append("-" * 62)
    lines.append("汇总")
    type_counts = {}
    for sig in signals:
        st = sig.get("signal_type_zh", "未知")
        type_counts[st] = type_counts.get(st, 0) + 1
    summary_parts = [f"{k}: {v}" for k, v in type_counts.items()]
    lines.append(f"  信号分布: {' | '.join(summary_parts)}")

    # Highlight most actionable
    emerging = [s for s in signals if s.get("signal_type") == "emerging"]
    accelerating = [s for s in signals if s.get("signal_type") == "accelerating"]
    if emerging:
        lines.append(f"  🆕 新兴信号 ({len(emerging)}):")
        for s in emerging:
            lines.append(f"     - {s['representative_title']}")
    if accelerating:
        lines.append(f"  📈 加速趋势 ({len(accelerating)}):")
        for s in accelerating:
            lines.append(f"     - {s['representative_title']} "
                         f"(×{s.get('acceleration', 0):.1f})")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate gray rhino trend-aware risk report",
        epilog="Examples:\n"
               "  python rhino_report.py --format text\n"
               "  python rhino_report.py --max-age 24 --top 5 --format json\n"
               "  python rhino_report.py --fetch-only\n"
               "  python rhino_report.py --category geopolitics\n"
               "  python rhino_report.py --lookback 14\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--max-age", type=int, default=48,
                        help="News age window in hours (default: 48)")
    parser.add_argument("--top", type=int, default=8,
                        help="Top N trend signals to report (default: 8)")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter by risk category (e.g., geopolitics, macro)")
    parser.add_argument("--min-cluster", type=int, default=1,
                        help="Minimum cluster size (default: 1)")
    parser.add_argument("--lookback", type=int, default=7,
                        help="Historical lookback window in days (default: 7)")
    parser.add_argument("--history-dir", type=str, default=None,
                        help="Custom history directory path")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not save today's snapshot to history")
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
        history_dir=args.history_dir,
        lookback_days=args.lookback,
        save_snapshot=not args.no_save,
    )

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report))


if __name__ == "__main__":
    main()
