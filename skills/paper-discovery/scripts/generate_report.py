#!/usr/bin/env python3
"""
Generate formatted paper report from JSON data
用于从 fetch_papers 的 JSON 输出生成格式化报告
"""

import argparse
import json
import sys
from datetime import datetime
from typing import List, Dict, Any


def load_papers_from_json(file_path: str) -> List[Dict[str, Any]]:
    """Load papers from JSON file"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading JSON: {e}", file=sys.stderr)
        sys.exit(1)


def format_paper_entry(paper: Dict[str, Any], index: int, show_source: bool = False) -> str:
    """Format a single paper entry"""
    lines = []
    
    heat_level = paper.get("heat_level", 1)
    heat_emoji = "🔥" * heat_level
    
    # Source indicator
    source_indicator = ""
    if show_source:
        if paper.get("source") == "huggingface":
            source_indicator = "[HF] "
        elif paper.get("source") == "arxiv":
            source_indicator = "[arXiv] "
    
    lines.append(f"{index}. {heat_emoji} {source_indicator}{paper['title']}")
    
    # Metrics line
    metrics = []
    if paper.get("upvotes", 0) > 0:
        metrics.append(f"👍 {paper['upvotes']} upvotes")
    if paper.get("github_stars"):
        metrics.append(f"⭐ {paper['github_stars']} GitHub stars")
    if metrics:
        lines.append(f"   {' | '.join(metrics)}")
    
    # AI Summary / Abstract (Chinese preferred)
    summary = ""
    if paper.get("ai_summary_zh"):
        summary = paper["ai_summary_zh"]
    elif paper.get("ai_summary"):
        summary = paper["ai_summary"]
    elif paper.get("abstract_zh"):
        summary = paper["abstract_zh"]
    elif paper.get("abstract"):
        summary = paper["abstract"]
    
    if summary:
        # Truncate to ~200 chars
        if len(summary) > 220:
            summary = summary[:220] + "..."
        lines.append(f"   📝 {summary}")
    
    # Keywords
    keywords = paper.get("ai_keywords", [])
    if keywords:
        keywords_str = ", ".join(keywords[:8])
        lines.append(f"   🏷️ 关键词: {keywords_str}")
    
    # Links
    links = []
    if paper.get("arxiv_url"):
        links.append(f"[arXiv]({paper['arxiv_url']})")
    if paper.get("pdf_url"):
        links.append(f"[PDF]({paper['pdf_url']})")
    if paper.get("hf_url"):
        links.append(f"[HuggingFace]({paper['hf_url']})")
    if paper.get("github_repo"):
        links.append(f"[GitHub]({paper['github_repo']})")
    
    if links:
        lines.append(f"   🔗 {' | '.join(links)}")
    
    lines.append("")
    return "\n".join(lines)


def generate_formatted_report(papers: List[Dict[str, Any]], group_by_source: bool = True) -> str:
    """Generate formatted markdown report"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"📚 今日 AI 论文热榜 ({today})",
        "=" * 60,
        ""
    ]
    
    if group_by_source:
        # Group papers by source
        papers_by_source: Dict[str, List[Dict[str, Any]]] = {}
        for paper in papers:
            source = paper.get("source", "unknown")
            if source not in papers_by_source:
                papers_by_source[source] = []
            papers_by_source[source].append(paper)
        
        # Source display names
        source_names = {
            "huggingface": "🤗 HuggingFace 热门论文",
            "arxiv": "📚 arXiv 最新论文"
        }
        
        # Display each source group
        for source in ["huggingface", "arxiv", "unknown"]:
            if source not in papers_by_source:
                continue
            
            source_papers = papers_by_source[source]
            display_name = source_names.get(source, f"📄 {source.title()}")
            
            lines.append(f"\n{display_name}")
            lines.append("-" * 40)
            lines.append("")
            
            for i, paper in enumerate(source_papers, 1):
                lines.append(format_paper_entry(paper, i, show_source=False))
    else:
        # Flat list
        for i, paper in enumerate(papers, 1):
            lines.append(format_paper_entry(paper, i, show_source=True))
    
    # Statistics
    lines.append("\n📊 统计汇总")
    lines.append("-" * 40)
    
    total = len(papers)
    lines.append(f"- 总论文数: {total} 篇")
    
    # Source breakdown
    source_counts = {}
    for p in papers:
        src = p.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1
    
    source_strs = []
    for src, count in source_counts.items():
        if src == "huggingface":
            source_strs.append(f"HuggingFace: {count}")
        elif src == "arxiv":
            source_strs.append(f"arXiv: {count}")
        else:
            source_strs.append(f"{src}: {count}")
    if source_strs:
        lines.append(f"- 来源分布: {' | '.join(source_strs)}")
    
    # Heat statistics
    high_heat = sum(1 for p in papers if p.get("heat_index", 0) >= 60)
    with_github = sum(1 for p in papers if p.get("github_repo"))
    
    lines.append(f"- 高热度论文 (≥60分): {high_heat} 篇")
    lines.append(f"- 有开源代码: {with_github} 篇")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate formatted paper report from JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("json_file", help="Path to JSON file containing paper data")
    parser.add_argument("--output", "-o", help="Output file (default: stdout)")
    parser.add_argument("--no-group", action="store_true", 
                       help="Don't group by source, show flat list")
    
    args = parser.parse_args()
    
    # Load papers
    papers = load_papers_from_json(args.json_file)
    
    # Generate report
    report = generate_formatted_report(papers, group_by_source=not args.no_group)
    
    # Output
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"Report saved to: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
