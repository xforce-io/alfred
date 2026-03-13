#!/usr/bin/env python3
"""News clustering and gray rhino event structuring.

This module clusters fetched news by topic and outputs structured data
for the Agent to evaluate which clusters constitute gray rhino risks.
"""

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk category keywords for pre-classification
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    "geopolitics": [
        "war", "military", "missile", "troops", "invasion", "sanctions",
        "conflict", "NATO", "nuclear", "ceasefire", "battle", "airstrike",
        "战争", "军事", "冲突", "制裁", "导弹", "核", "入侵", "停火",
        "Iran", "伊朗", "Russia", "俄罗斯", "Ukraine", "乌克兰", "Taiwan", "台湾",
        "Israel", "以色列", "Gaza", "加沙", "North Korea", "朝鲜",
    ],
    "macro": [
        "Fed", "ECB", "BOJ", "interest rate", "inflation", "CPI", "GDP",
        "recession", "default", "sovereign debt", "quantitative",
        "美联储", "央行", "加息", "降息", "通胀", "衰退", "债务", "缩表",
        "treasury", "yield", "bond", "国债", "收益率",
    ],
    "trade": [
        "tariff", "trade war", "sanctions", "export ban", "supply chain",
        "decoupling", "reshoring", "chip ban",
        "关税", "贸易战", "出口管制", "供应链", "脱钩", "芯片",
    ],
    "tech_regulation": [
        "AI regulation", "antitrust", "monopoly", "data privacy", "GDPR",
        "crypto ban", "SEC", "compliance",
        "AI监管", "反垄断", "数据安全", "隐私", "加密货币监管",
    ],
    "energy_climate": [
        "OPEC", "oil price", "natural gas", "pipeline", "energy crisis",
        "climate", "carbon", "renewable", "nuclear power",
        "OPEC", "油价", "天然气", "管道", "能源危机", "气候", "碳",
        "hurricane", "earthquake", "flood", "drought",
        "飓风", "地震", "洪水", "干旱",
    ],
    "health_social": [
        "pandemic", "epidemic", "virus", "outbreak", "WHO", "vaccine",
        "疫情", "病毒", "世卫", "疫苗", "感染",
    ],
}

CATEGORY_NAMES_ZH = {
    "geopolitics": "地缘政治",
    "macro": "宏观经济",
    "trade": "贸易与产业",
    "tech_regulation": "科技监管",
    "energy_climate": "能源与气候",
    "health_social": "公共卫生与社会",
    "other": "其他",
}


@dataclass
class NewsCluster:
    """A cluster of related news items on the same topic."""
    cluster_id: int
    category: str
    category_zh: str
    representative_title: str
    titles: List[str]
    summaries: List[str]
    sources: List[str]
    count: int
    keywords: List[str] = field(default_factory=list)


class RhinoAnalyzer:
    """Clusters news items and prepares structured data for LLM evaluation."""

    def __init__(self, min_cluster_size: int = 1):
        self.min_cluster_size = min_cluster_size

    def analyze(self, news_items: List[dict]) -> List[dict]:
        """Cluster news and return structured clusters for LLM evaluation.

        Args:
            news_items: List of news item dicts (from NewsFetcher output).

        Returns:
            List of cluster dicts ready for LLM gray rhino evaluation.
        """
        # Step 1: Classify each item
        classified = self._classify_items(news_items)

        # Step 2: Cluster within each category
        clusters = self._cluster_by_similarity(classified)

        # Step 3: Filter small clusters and sort by size
        clusters = [c for c in clusters if c.count >= self.min_cluster_size]
        clusters.sort(key=lambda x: x.count, reverse=True)

        return [asdict(c) for c in clusters]

    def _classify_items(self, items: List[dict]) -> List[dict]:
        """Add category labels to news items based on keyword matching."""
        for item in items:
            text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
            best_cat = "other"
            best_score = 0
            for cat, keywords in CATEGORY_KEYWORDS.items():
                score = sum(1 for kw in keywords if kw.lower() in text)
                if score > best_score:
                    best_score = score
                    best_cat = cat
            item["category"] = best_cat
        return items

    def _cluster_by_similarity(self, items: List[dict]) -> List[NewsCluster]:
        """Cluster items within each category by keyword overlap."""
        # Group by category first
        by_category: Dict[str, List[dict]] = defaultdict(list)
        for item in items:
            by_category[item["category"]].append(item)

        all_clusters = []
        cluster_id = 0

        for category, cat_items in by_category.items():
            # Try TF-IDF clustering if sklearn available, else fallback
            sub_clusters = self._keyword_cluster(cat_items)

            for titles_group in sub_clusters:
                if len(titles_group) < 1:
                    continue
                group_items = [
                    it for it in cat_items
                    if it.get("title", "") in titles_group
                ]
                if not group_items:
                    continue

                # Extract common keywords
                keywords = self._extract_keywords(group_items)

                cluster_id += 1
                all_clusters.append(NewsCluster(
                    cluster_id=cluster_id,
                    category=category,
                    category_zh=CATEGORY_NAMES_ZH.get(category, category),
                    representative_title=group_items[0].get("title", ""),
                    titles=[it.get("title", "") for it in group_items],
                    summaries=[it.get("summary", "")[:200] for it in group_items if it.get("summary")],
                    sources=list(set(it.get("source", "") for it in group_items)),
                    count=len(group_items),
                    keywords=keywords[:10],
                ))

        return all_clusters

    def _keyword_cluster(self, items: List[dict]) -> List[List[str]]:
        """Cluster items using TF-IDF + agglomerative clustering, or fallback."""
        titles = [it.get("title", "") for it in items]
        if len(titles) < 2:
            return [titles] if titles else []

        try:
            return self._tfidf_cluster(titles)
        except ImportError:
            logger.info("sklearn not available, using keyword fallback clustering")
            return self._fallback_cluster(items)

    def _tfidf_cluster(self, titles: List[str]) -> List[List[str]]:
        """TF-IDF based clustering with distance threshold."""
        from sklearn.cluster import AgglomerativeClustering
        from sklearn.feature_extraction.text import TfidfVectorizer

        vectorizer = TfidfVectorizer(
            max_features=500,
            stop_words="english",
            token_pattern=r'(?u)\b\w+\b',  # include CJK chars
        )
        tfidf_matrix = vectorizer.fit_transform(titles)

        # Use distance_threshold instead of fixed n_clusters
        # This avoids lumping unrelated items into one mega-cluster
        clustering = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=0.9,
            metric="cosine",
            linkage="average",
        )
        labels = clustering.fit_predict(tfidf_matrix.toarray())

        groups: Dict[int, List[str]] = defaultdict(list)
        for title, label in zip(titles, labels):
            groups[label].append(title)
        return list(groups.values())

    def _fallback_cluster(self, items: List[dict]) -> List[List[str]]:
        """Simple keyword-overlap clustering fallback."""
        clusters: List[List[str]] = []
        assigned = set()

        for i, item_a in enumerate(items):
            if i in assigned:
                continue
            title_a = item_a.get("title", "")
            words_a = set(re.findall(r'\w{3,}', title_a.lower()))
            cluster = [title_a]
            assigned.add(i)

            for j, item_b in enumerate(items):
                if j in assigned:
                    continue
                title_b = item_b.get("title", "")
                words_b = set(re.findall(r'\w{3,}', title_b.lower()))
                if words_a and words_b:
                    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
                    if overlap > 0.3:
                        cluster.append(title_b)
                        assigned.add(j)
            clusters.append(cluster)
        return clusters

    def _extract_keywords(self, items: List[dict]) -> List[str]:
        """Extract most frequent meaningful words from a group."""
        word_freq: Dict[str, int] = defaultdict(int)
        stopwords = {"the", "a", "an", "in", "on", "at", "to", "for", "of",
                     "is", "are", "was", "were", "and", "or", "but", "with",
                     "from", "by", "as", "it", "its", "has", "have", "had",
                     "this", "that", "will", "be", "been", "not", "no",
                     "says", "said", "new", "after", "over", "could", "may"}
        for item in items:
            text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
            words = re.findall(r'\b[\w\u4e00-\u9fff]{2,}\b', text)
            for w in words:
                if w not in stopwords and not w.isdigit():
                    word_freq[w] += 1
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in sorted_words[:15]]


def main():
    parser = argparse.ArgumentParser(
        description="Cluster news items and prepare for gray rhino evaluation"
    )
    parser.add_argument("--input", type=str, default="-",
                        help="Input JSON file (- for stdin, default: stdin)")
    parser.add_argument("--min-cluster", type=int, default=2,
                        help="Minimum cluster size (default: 2)")
    parser.add_argument("--format", choices=["json", "text"], default="text",
                        help="Output format")
    args = parser.parse_args()

    # Read input
    if args.input == "-":
        raw = sys.stdin.read()
    else:
        with open(args.input) as f:
            raw = f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON input: {e}"},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    items = data.get("items", data) if isinstance(data, dict) else data

    analyzer = RhinoAnalyzer(min_cluster_size=args.min_cluster)
    clusters = analyzer.analyze(items)

    if args.format == "json":
        print(json.dumps({"ok": True, "cluster_count": len(clusters),
                          "clusters": clusters},
                         ensure_ascii=False, indent=2))
    else:
        print(f"Found {len(clusters)} news clusters\n")
        for c in clusters:
            icon = {"geopolitics": "🌍", "macro": "💰", "trade": "🚢",
                    "tech_regulation": "🤖", "energy_climate": "⚡",
                    "health_social": "🏥"}.get(c["category"], "📰")
            print(f"{icon} [{c['category_zh']}] {c['representative_title']}")
            print(f"   相关新闻: {c['count']} 条 | 来源: {', '.join(c['sources'])}")
            print(f"   关键词: {', '.join(c['keywords'][:5])}")
            if len(c["titles"]) > 1:
                for t in c["titles"][1:3]:
                    print(f"   · {t}")
                if len(c["titles"]) > 3:
                    print(f"   · ...及 {len(c['titles'])-3} 条更多")
            print()


if __name__ == "__main__":
    main()
