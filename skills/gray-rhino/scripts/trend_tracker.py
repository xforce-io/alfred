#!/usr/bin/env python3
"""Trend tracking and historical baseline for gray rhino analysis.

Stores daily cluster snapshots and computes trend signals:
- Acceleration: is a topic's coverage growing faster than before?
- Novelty: is this a newly emerging topic?
- Multi-source: are independent sources converging on the same topic?

This shifts focus from "what's hot now" (already priced in) to
"what's emerging" (actionable gray rhino signals).
"""

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Default history directory
DEFAULT_HISTORY_DIR = os.path.expanduser("~/.alfred/gray-rhino/history")


@dataclass
class TrendSignal:
    """Trend analysis result for a single cluster."""
    cluster_id: int
    representative_title: str
    category: str
    category_zh: str
    keywords: List[str]
    sources: List[str]
    current_count: int

    # Trend metrics
    signal_type: str  # "emerging" | "accelerating" | "steady" | "fading"
    signal_type_zh: str
    trend_score: float  # composite score, higher = more actionable
    novelty_score: float  # 0-1, 1 = brand new topic
    acceleration: float  # >1 = growing, <1 = shrinking, 0 = new
    source_diversity: float  # 0-1, 1 = many independent sources
    days_tracked: int  # how many days this topic has appeared
    history_counts: List[int]  # daily counts over tracking window


@dataclass
class DailySnapshot:
    """A single day's cluster data for history storage."""
    date: str  # YYYY-MM-DD
    clusters: List[dict]  # simplified cluster data for storage


class TrendTracker:
    """Tracks topic trends over time using local history files."""

    def __init__(self, history_dir: str = DEFAULT_HISTORY_DIR,
                 lookback_days: int = 7):
        self.history_dir = Path(history_dir)
        self.lookback_days = lookback_days
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def save_snapshot(self, clusters: List[dict], date: str = None):
        """Save today's clusters as a historical snapshot."""
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Store only essential fields to keep files small
        simplified = []
        for c in clusters:
            simplified.append({
                "keywords": c.get("keywords", [])[:10],
                "category": c.get("category", ""),
                "count": c.get("count", 1),
                "sources": c.get("sources", []),
                "representative_title": c.get("representative_title", ""),
            })

        snapshot = {"date": date, "clusters": simplified}
        filepath = self.history_dir / f"{date}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved snapshot: {filepath} ({len(simplified)} clusters)")

    def load_history(self, before_date: str = None) -> List[DailySnapshot]:
        """Load historical snapshots within the lookback window."""
        if before_date is None:
            before_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        cutoff = datetime.strptime(before_date, "%Y-%m-%d") - timedelta(
            days=self.lookback_days)

        snapshots = []
        for filepath in sorted(self.history_dir.glob("*.json")):
            try:
                file_date = filepath.stem  # YYYY-MM-DD
                dt = datetime.strptime(file_date, "%Y-%m-%d")
                if dt < cutoff or file_date >= before_date:
                    continue
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                snapshots.append(DailySnapshot(
                    date=data["date"],
                    clusters=data.get("clusters", []),
                ))
            except (ValueError, json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Skipping corrupt history file {filepath}: {e}")
        return snapshots

    def analyze_trends(self, current_clusters: List[dict]) -> List[TrendSignal]:
        """Compare current clusters against historical baseline.

        Returns clusters ranked by trend signal strength (most actionable first).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = self.load_history(before_date=today)

        # Build historical keyword->count timeline
        historical_topics = self._build_topic_timeline(history)

        signals = []
        for cluster in current_clusters:
            signal = self._compute_signal(cluster, historical_topics, history)
            signals.append(signal)

        # Sort by trend_score descending (most actionable first)
        signals.sort(key=lambda s: s.trend_score, reverse=True)
        return signals

    def _build_topic_timeline(self, history: List[DailySnapshot]
                              ) -> Dict[str, List[Tuple[str, int]]]:
        """Build a mapping from keyword-set fingerprint to (date, count) pairs.

        We use top keywords to match topics across days, since titles change
        but the underlying topic's keywords remain similar.
        """
        timeline: Dict[str, List[Tuple[str, int]]] = defaultdict(list)

        for snapshot in history:
            for cluster in snapshot.clusters:
                fp = self._topic_fingerprint(cluster.get("keywords", []),
                                             cluster.get("category", ""))
                if fp:
                    timeline[fp].append((
                        snapshot.date,
                        cluster.get("count", 1),
                    ))
        return timeline

    def _topic_fingerprint(self, keywords: List[str], category: str) -> str:
        """Create a fuzzy fingerprint for a topic based on top keywords."""
        if not keywords:
            return ""
        # Use category + top 5 sorted keywords as fingerprint
        top = sorted([k.lower() for k in keywords[:5]])
        return f"{category}:{','.join(top)}"

    def _find_matching_history(self, cluster: dict,
                               historical_topics: Dict[str, List[Tuple[str, int]]]
                               ) -> Tuple[List[Tuple[str, int]], float]:
        """Find the best matching historical topic for a cluster.

        Returns (timeline, match_quality) where match_quality is 0-1.
        """
        current_keywords = set(k.lower() for k in cluster.get("keywords", [])[:8])
        category = cluster.get("category", "")

        if not current_keywords:
            return [], 0.0

        best_timeline = []
        best_overlap = 0.0

        for fp, timeline in historical_topics.items():
            fp_cat, fp_kws_str = fp.split(":", 1) if ":" in fp else ("", fp)
            fp_kws = set(fp_kws_str.split(","))

            # Must be same category
            if fp_cat != category:
                continue

            overlap = len(current_keywords & fp_kws)
            if not fp_kws:
                continue
            overlap_ratio = overlap / min(len(current_keywords), len(fp_kws))

            if overlap_ratio > best_overlap:
                best_overlap = overlap_ratio
                best_timeline = timeline

        # Require at least 40% keyword overlap to consider it the same topic
        if best_overlap < 0.4:
            return [], 0.0

        return best_timeline, best_overlap

    def _compute_signal(self, cluster: dict,
                        historical_topics: Dict[str, List[Tuple[str, int]]],
                        history: List[DailySnapshot]) -> TrendSignal:
        """Compute trend signal for a single cluster."""
        current_count = cluster.get("count", 1)
        keywords = cluster.get("keywords", [])
        sources = cluster.get("sources", [])

        # Find historical match
        timeline, match_quality = self._find_matching_history(
            cluster, historical_topics)

        # --- Novelty Score ---
        # Brand new topic = 1.0, seen every day in lookback = 0.0
        days_tracked = len(set(date for date, _ in timeline))
        total_history_days = max(len(history), 1)
        novelty_score = 1.0 - (days_tracked / total_history_days)

        # --- Acceleration ---
        # Compare recent counts vs older counts
        history_counts = self._build_daily_counts(timeline, history)
        acceleration = self._compute_acceleration(history_counts, current_count)

        # --- Source Diversity ---
        # More independent sources = stronger signal
        num_sources = len(set(sources))
        source_diversity = min(num_sources / 4.0, 1.0)  # cap at 4 sources

        # --- Composite Trend Score ---
        # Heavily weight novelty and acceleration, moderately weight source diversity
        # Penalize high absolute count (already mainstream)
        mainstream_penalty = 1.0 / (1.0 + max(0, current_count - 3) * 0.2)
        trend_score = (
            novelty_score * 3.0 +
            max(acceleration - 1.0, 0) * 2.0 +  # only reward growth
            source_diversity * 1.5
        ) * mainstream_penalty

        # --- Signal Type ---
        signal_type, signal_type_zh = self._classify_signal(
            novelty_score, acceleration, days_tracked, current_count)

        return TrendSignal(
            cluster_id=cluster.get("cluster_id", 0),
            representative_title=cluster.get("representative_title", ""),
            category=cluster.get("category", ""),
            category_zh=cluster.get("category_zh", ""),
            keywords=keywords,
            sources=sources,
            current_count=current_count,
            signal_type=signal_type,
            signal_type_zh=signal_type_zh,
            trend_score=round(trend_score, 2),
            novelty_score=round(novelty_score, 2),
            acceleration=round(acceleration, 2),
            source_diversity=round(source_diversity, 2),
            days_tracked=days_tracked,
            history_counts=history_counts,
        )

    def _build_daily_counts(self, timeline: List[Tuple[str, int]],
                            history: List[DailySnapshot]) -> List[int]:
        """Build an ordered list of daily counts over the lookback window."""
        if not history:
            return []
        date_counts = defaultdict(int)
        for date, count in timeline:
            date_counts[date] += count

        # Fill in zeros for days with no matches
        all_dates = sorted(set(s.date for s in history))
        return [date_counts.get(d, 0) for d in all_dates]

    def _compute_acceleration(self, history_counts: List[int],
                              current_count: int) -> float:
        """Compute acceleration as ratio of current vs recent average.

        Returns:
            > 1.0: growing
            = 1.0: stable
            < 1.0: shrinking
            0.0: no history (brand new)
        """
        if not history_counts:
            return 0.0  # no history = brand new

        # Split history into older half and recent half
        n = len(history_counts)
        if n >= 2:
            mid = n // 2
            older_avg = sum(history_counts[:mid]) / mid
            recent_avg = sum(history_counts[mid:]) / (n - mid)
            # Include current count in "recent" signal
            recent_with_current = (recent_avg + current_count) / 2.0
            baseline = max(older_avg, 0.5)  # avoid division by zero
            return recent_with_current / baseline
        else:
            # Only 1 day of history
            baseline = max(history_counts[0], 0.5)
            return current_count / baseline

    def _classify_signal(self, novelty: float, acceleration: float,
                         days_tracked: int, count: int
                         ) -> Tuple[str, str]:
        """Classify the trend signal into a human-readable type."""
        if novelty >= 0.8 and days_tracked <= 1:
            return "emerging", "新兴信号"
        elif acceleration > 1.5:
            return "accelerating", "加速趋势"
        elif acceleration > 0.8:
            if count >= 5:
                return "steady", "持续热点"
            return "steady", "平稳关注"
        else:
            return "fading", "趋势减弱"
