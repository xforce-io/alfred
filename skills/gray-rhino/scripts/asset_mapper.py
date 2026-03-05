#!/usr/bin/env python3
"""Map gray rhino events to asset impact matrix."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Asset definitions
# ---------------------------------------------------------------------------
ASSET_CLASSES = {
    "原油": {"symbols": ["WTI", "Brent"], "type": "commodity"},
    "黄金": {"symbols": ["XAU"], "type": "safe_haven"},
    "美债": {"symbols": ["US10Y", "US2Y"], "type": "bond"},
    "美元": {"symbols": ["DXY"], "type": "currency"},
    "A股": {"symbols": ["上证", "创业板"], "type": "equity_cn"},
    "港股": {"symbols": ["恒生", "恒生科技"], "type": "equity_hk"},
    "美股": {"symbols": ["SPX", "NDX"], "type": "equity_us"},
    "BTC": {"symbols": ["BTC"], "type": "crypto"},
    "铜": {"symbols": ["HG"], "type": "commodity"},
}

# ---------------------------------------------------------------------------
# Historical impact rules: scenario -> {asset: direction}
# Directions: ↑↑ (strong up), ↑ (up), ↓ (down), ↓↓ (strong down), ± (neutral)
# ---------------------------------------------------------------------------
IMPACT_RULES: Dict[str, Dict[str, str]] = {
    # Geopolitics
    "中东冲突": {"原油": "↑↑", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "↓"},
    "俄乌冲突": {"原油": "↑", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "↓"},
    "台海紧张": {"原油": "↑", "黄金": "↑↑", "美债": "↑", "美元": "↑", "A股": "↓↓", "港股": "↓↓", "美股": "↓", "BTC": "±", "铜": "↓"},
    "朝鲜半岛": {"原油": "↑", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "↓"},

    # Macro
    "美联储加息": {"原油": "↓", "黄金": "↓", "美债": "↓↓", "美元": "↑↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "↓", "铜": "↓"},
    "美联储降息": {"原油": "↑", "黄金": "↑", "美债": "↑↑", "美元": "↓", "A股": "↑", "港股": "↑", "美股": "↑", "BTC": "↑", "铜": "↑"},
    "日央行加息": {"原油": "↓", "黄金": "±", "美债": "↓", "美元": "↓", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "↓", "铜": "↓"},
    "欧洲债务危机": {"原油": "↓", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "↓"},
    "银行危机": {"原油": "↓", "黄金": "↑↑", "美债": "↑↑", "美元": "±", "A股": "↓↓", "港股": "↓", "美股": "↓↓", "BTC": "↑", "铜": "↓"},
    "通胀失控": {"原油": "↑", "黄金": "↑↑", "美债": "↓↓", "美元": "↓", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "↑", "铜": "↑"},

    # Trade
    "中美贸易战": {"原油": "↓", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓↓", "港股": "↓↓", "美股": "↓", "BTC": "±", "铜": "↓↓"},
    "芯片出口管制": {"原油": "±", "黄金": "±", "美债": "±", "美元": "±", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "±"},
    "供应链断裂": {"原油": "↑", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "↑"},

    # Energy & Climate
    "OPEC减产": {"原油": "↑↑", "黄金": "±", "美债": "±", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "±"},
    "能源危机": {"原油": "↑↑", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "↓"},
    "极端天气": {"原油": "↑", "黄金": "±", "美债": "↑", "美元": "±", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "±"},

    # Tech regulation
    "AI严格监管": {"原油": "±", "黄金": "±", "美债": "±", "美元": "±", "A股": "↓", "港股": "↓", "美股": "↓", "BTC": "±", "铜": "±"},
    "加密货币监管": {"原油": "±", "黄金": "↑", "美债": "±", "美元": "±", "A股": "±", "港股": "±", "美股": "±", "BTC": "↓↓", "铜": "±"},

    # Health
    "全球疫情": {"原油": "↓↓", "黄金": "↑", "美债": "↑↑", "美元": "↑", "A股": "↓↓", "港股": "↓↓", "美股": "↓↓", "BTC": "±", "铜": "↓↓"},

    # China specific
    "中国大规模刺激": {"原油": "↑", "黄金": "±", "美债": "±", "美元": "↓", "A股": "↑↑", "港股": "↑↑", "美股": "±", "BTC": "±", "铜": "↑↑"},
    "中国房地产危机": {"原油": "↓", "黄金": "↑", "美债": "↑", "美元": "↑", "A股": "↓↓", "港股": "↓↓", "美股": "↓", "BTC": "±", "铜": "↓↓"},
}

# Keywords to match scenarios
SCENARIO_KEYWORDS: Dict[str, List[str]] = {
    "中东冲突": ["iran", "伊朗", "美伊", "israel", "以色列", "gaza", "加沙", "yemen", "也门", "houthi", "胡塞", "hezbollah", "真主党", "hormuz", "霍尔木兹", "中东", "middle east"],
    "俄乌冲突": ["russia", "俄罗斯", "ukraine", "乌克兰", "moscow", "莫斯科", "kyiv", "基辅"],
    "台海紧张": ["taiwan", "台湾", "台海", "strait"],
    "朝鲜半岛": ["north korea", "朝鲜", "pyongyang", "平壤"],
    "美联储加息": ["fed hike", "fed raise", "美联储加息", "rate hike", "hawkish fed", "federal reserve"],
    "美联储降息": ["fed cut", "美联储降息", "rate cut", "dovish fed", "federal reserve"],
    "日央行加息": ["boj", "日本央行", "日央行", "japan rate", "日本加息", "日央行加息", "yen", "日元"],
    "欧洲债务危机": ["europe debt", "欧洲债务", "greek", "希腊", "italy debt", "意大利债务"],
    "银行危机": ["bank crisis", "银行危机", "bank run", "挤兑", "svb", "credit suisse"],
    "通胀失控": ["hyperinflation", "恶性通胀", "stagflation", "滞胀", "cpi surge"],
    "中美贸易战": ["trade war", "贸易战", "tariff", "关税", "us china trade"],
    "芯片出口管制": ["chip ban", "芯片禁令", "semiconductor restriction", "半导体管制", "chip export"],
    "供应链断裂": ["supply chain", "供应链", "logistics crisis", "物流危机"],
    "OPEC减产": ["opec cut", "opec减产", "production cut", "减产"],
    "能源危机": ["energy crisis", "能源危机", "power shortage", "电力短缺", "gas shortage"],
    "极端天气": ["hurricane", "飓风", "earthquake", "地震", "flood", "洪水", "drought", "干旱", "wildfire"],
    "AI严格监管": ["ai regulation", "ai监管", "ai ban", "ai law", "ai act"],
    "加密货币监管": ["crypto regulation", "加密货币监管", "crypto ban", "sec crypto"],
    "全球疫情": ["pandemic", "疫情", "outbreak", "爆发", "who emergency", "世卫"],
    "中国大规模刺激": ["china stimulus", "中国刺激", "中国放水", "pboc cut", "降准"],
    "中国房地产危机": ["china property", "中国房地产", "恒大", "evergrande", "碧桂园", "country garden"],
}


@dataclass
class AssetImpact:
    asset: str
    direction: str  # ↑↑, ↑, ↓, ↓↓, ±
    confidence: str  # high, medium, low


@dataclass
class EventImpact:
    event_name: str
    category: str
    matched_scenario: str
    impacts: List[AssetImpact]


@dataclass
class ImpactMatrix:
    events: List[EventImpact]
    generated_at: str = ""


class AssetMapper:
    """Maps gray rhino events to asset impact directions."""

    def __init__(self):
        self.rules = IMPACT_RULES
        self.keywords = SCENARIO_KEYWORDS

    def match_scenario(self, event_name: str, keywords: List[str] = None) -> Optional[str]:
        """Find the best matching scenario for an event."""
        text = event_name.lower()
        if keywords:
            text += " " + " ".join(kw.lower() for kw in keywords)

        best_match = None
        best_score = 0
        for scenario, kws in self.keywords.items():
            score = sum(1 for kw in kws if kw.lower() in text)
            if score > best_score:
                best_score = score
                best_match = scenario
        return best_match if best_score > 0 else None

    def map_event(self, event_name: str, category: str = "",
                  keywords: List[str] = None) -> Optional[EventImpact]:
        """Map a single event to asset impacts."""
        scenario = self.match_scenario(event_name, keywords)
        if not scenario or scenario not in self.rules:
            return None

        impacts = []
        for asset, direction in self.rules[scenario].items():
            confidence = "high" if direction in ("↑↑", "↓↓") else \
                        "medium" if direction in ("↑", "↓") else "low"
            impacts.append(AssetImpact(
                asset=asset,
                direction=direction,
                confidence=confidence,
            ))
        return EventImpact(
            event_name=event_name,
            category=category,
            matched_scenario=scenario,
            impacts=impacts,
        )

    def map_clusters(self, clusters: List[dict]) -> ImpactMatrix:
        """Map multiple clusters to an impact matrix."""
        from datetime import datetime, timezone
        events = []
        for cluster in clusters:
            name = cluster.get("representative_title", "")
            category = cluster.get("category_zh", "")
            keywords = cluster.get("keywords", [])
            # Add all titles as context for matching
            all_text = " ".join(cluster.get("titles", []))
            combined_keywords = keywords + all_text.split()

            impact = self.map_event(name, category, combined_keywords)
            if impact:
                events.append(impact)

        return ImpactMatrix(
            events=events,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def format_matrix_text(self, matrix: ImpactMatrix) -> str:
        """Format impact matrix as readable text table."""
        if not matrix.events:
            return "No asset impacts mapped for current events."

        lines = []
        assets = list(ASSET_CLASSES.keys())

        for event in matrix.events:
            impact_map = {i.asset: i.direction for i in event.impacts}
            lines.append(f"📊 {event.event_name}")
            lines.append(f"   匹配场景: {event.matched_scenario}")
            lines.append(f"   ┌{'─' * 11}┬{'─' * 8}┐")
            lines.append(f"   │ {'资产':<8} │ {'影响':<5} │")
            lines.append(f"   ├{'─' * 11}┼{'─' * 8}┤")
            for asset in assets:
                direction = impact_map.get(asset, "—")
                lines.append(f"   │ {asset:<8} │ {direction:<5} │")
            lines.append(f"   └{'─' * 11}┴{'─' * 8}┘")
            lines.append("")

        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Map events/clusters to asset impact matrix"
    )
    parser.add_argument("--input", type=str, default="-",
                        help="Input JSON (clusters from rhino_analyzer, - for stdin)")
    parser.add_argument("--event", type=str, default=None,
                        help="Single event name to map (skip cluster input)")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="List all built-in scenarios and exit")
    args = parser.parse_args()

    if args.list_scenarios:
        print("Built-in risk scenarios:\n")
        for scenario, impacts in IMPACT_RULES.items():
            summary = " | ".join(f"{a}:{d}" for a, d in impacts.items() if d != "±")
            print(f"  {scenario}: {summary}")
        return

    mapper = AssetMapper()

    if args.event:
        impact = mapper.map_event(args.event)
        if impact:
            if args.format == "json":
                print(json.dumps({"ok": True, "event": asdict(impact)},
                                 ensure_ascii=False, indent=2))
            else:
                matrix = ImpactMatrix(events=[impact])
                print(mapper.format_matrix_text(matrix))
        else:
            print(json.dumps({"ok": False, "error": "No matching scenario found"},
                             ensure_ascii=False))
        return

    # Read cluster input
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

    clusters = data.get("clusters", data) if isinstance(data, dict) else data

    matrix = mapper.map_clusters(clusters)

    if args.format == "json":
        print(json.dumps({"ok": True, "matrix": asdict(matrix)},
                         ensure_ascii=False, indent=2))
    else:
        print(mapper.format_matrix_text(matrix))


if __name__ == "__main__":
    main()
