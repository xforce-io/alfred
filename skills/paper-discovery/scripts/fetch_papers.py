#!/usr/bin/env python3
"""
Paper Discovery Script
Fetch trending AI/ML papers from HuggingFace JSON API with arXiv fallback.
"""

import argparse
import json
import math
import re
import sys
from datetime import datetime
from typing import List, Dict, Any

import requests


def generate_one_line_summary(abstract: str) -> str:
    """Generate a one-line Chinese summary for a paper using LLM."""
    if not abstract:
        return ""

    try:
        import asyncio
        from dolphin.core.llm.llm_client import LLMClient
        from dolphin.core.common.enums import Messages as DolphinMessages, MessageRole
        from dolphin.core.context import Context

        context = Context()
        llm_client = LLMClient(context)
        msgs = DolphinMessages()
        prompt = (
            "请用一句简洁的中文概括以下论文摘要的核心贡献或发现，不超过50个字：\n\n"
            f"{abstract}"
        )
        msgs.append_message(MessageRole.USER, prompt)

        config = context.get_config()
        model = getattr(config, "fast_llm", None) or "qwen-turbo"

        async def _call():
            result = ""
            async for chunk in llm_client.mf_chat_stream(
                messages=msgs,
                model=model,
                temperature=0.3,
                no_cache=True,
            ):
                result = chunk.get("content") or ""
            return result.strip()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_call())
        finally:
            loop.close()
    except Exception as e:
        print(f"Warning: Failed to generate summary: {e}", file=sys.stderr)
        return ""


def fetch_huggingface_papers(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch trending papers from HuggingFace Daily Papers JSON API."""
    url = f"https://huggingface.co/api/daily_papers?limit={limit}"

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"Error fetching HuggingFace papers: {e}", file=sys.stderr)
        return []

    papers = []
    for item in data[:limit]:
        paper = item.get("paper", {})
        paper_id = paper.get("id", "")
        title = paper.get("title", "").strip().replace("\n", " ")
        if not paper_id or not title:
            continue

        authors = [a.get("name", "").strip() for a in paper.get("authors", []) if a.get("name")]
        abstract = paper.get("summary", "").strip().replace("\n", " ")
        published_date = (paper.get("publishedAt") or "")[:10]

        entry = {
            "paper_id": paper_id,
            "title": title,
            "authors": authors,
            "abstract": abstract[:500] + "..." if len(abstract) > 500 else abstract,
            "upvotes": paper.get("upvotes", 0),
            "hf_url": f"https://huggingface.co/papers/{paper_id}",
            "arxiv_url": f"https://arxiv.org/abs/{paper_id}",
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
            "source": "huggingface",
            "published_date": published_date,
            "fetched_at": datetime.now().isoformat(),
        }

        # Extra fields from HuggingFace API
        if paper.get("ai_summary"):
            entry["ai_summary"] = paper["ai_summary"]
        if paper.get("ai_keywords"):
            entry["ai_keywords"] = paper["ai_keywords"]
        if item.get("githubRepo"):
            repo = item["githubRepo"]
            entry["github_repo"] = repo.get("url", "")
            entry["github_stars"] = repo.get("stars", 0)

        papers.append(entry)

    # Sort by upvotes
    papers.sort(key=lambda x: x.get("upvotes", 0), reverse=True)
    return papers[:limit]


def fetch_arxiv_papers(category: str = "cs.AI", limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch recent papers from arXiv Atom API (fallback source)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Error: beautifulsoup4 is required for arXiv fetching. "
              "Install it with: pip install beautifulsoup4 lxml", file=sys.stderr)
        return []

    api_url = (
        f"https://export.arxiv.org/api/query?search_query=cat:{category}"
        f"&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
    )

    try:
        response = requests.get(api_url, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching arXiv papers: {e}", file=sys.stderr)
        return []

    try:
        soup = BeautifulSoup(response.text, "xml")
    except Exception as e:
        print(f"Error parsing arXiv XML response: {e}", file=sys.stderr)
        return []
    entries = soup.find_all("entry")
    papers = []

    for entry in entries[:limit]:
        id_elem = entry.find("id")
        if not id_elem:
            continue

        arxiv_id = id_elem.text.split("/abs/")[-1]
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)

        title_elem = entry.find("title")
        title = title_elem.text.strip().replace("\n", " ") if title_elem else ""

        authors = []
        for author in entry.find_all("author"):
            name_elem = author.find("name")
            if name_elem:
                authors.append(name_elem.text.strip())

        abstract_elem = entry.find("summary")
        abstract = abstract_elem.text.strip() if abstract_elem else ""

        published_elem = entry.find("published")
        published_date = published_elem.text[:10] if published_elem else ""

        categories = [cat.get("term") for cat in entry.find_all("category") if cat.get("term")]

        papers.append({
            "paper_id": arxiv_id,
            "title": title,
            "authors": authors,
            "abstract": abstract[:500] + "..." if len(abstract) > 500 else abstract,
            "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "source": "arxiv",
            "published_date": published_date,
            "category": category,
            "categories": categories,
            "fetched_at": datetime.now().isoformat(),
        })

    return papers


def calculate_heat_index(paper: Dict[str, Any]) -> float:
    """Calculate paper heat index (0-100)."""
    score = 0.0

    # Upvotes contribution
    upvotes = paper.get("upvotes", 0)
    if upvotes > 0:
        score += min(60, math.log(upvotes + 1) * 15)

    # GitHub stars bonus
    stars = paper.get("github_stars", 0)
    if stars > 0:
        score += min(15, math.log(stars + 1) * 3)

    # Freshness
    published_date = paper.get("published_date", "")
    if published_date:
        try:
            pub_date = datetime.strptime(published_date, "%Y-%m-%d")
            days_old = (datetime.now() - pub_date).days
            if days_old <= 1:
                score += 30
            elif days_old <= 3:
                score += 25
            elif days_old <= 7:
                score += 20
            elif days_old <= 14:
                score += 10
            else:
                score += 5
        except (ValueError, TypeError):
            score += 15
    else:
        score += 25

    # Source weight
    if paper.get("source") == "huggingface":
        score += 10
    elif paper.get("source") == "arxiv":
        score += 5

    return min(100, score)


def calculate_heat_level(heat_index: float) -> int:
    """Calculate heat level (1-5 fire emojis)."""
    if heat_index >= 80:
        return 5
    elif heat_index >= 60:
        return 4
    elif heat_index >= 40:
        return 3
    elif heat_index >= 20:
        return 2
    else:
        return 1


def display_report(papers: List[Dict[str, Any]]) -> None:
    """Display papers in structured daily-digest report format."""
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"📚 今日 AI 论文热榜 ({today})")
    print()

    for i, paper in enumerate(papers, 1):
        heat_level = paper.get("heat_level", calculate_heat_level(paper.get("heat_index", 0)))
        heat_emoji = "🔥" * heat_level

        print(f"{i}. {heat_emoji} {paper['title']}")

        # Stats line
        stats = []
        if paper.get("upvotes", 0) > 0:
            stats.append(f"👍 {paper['upvotes']} upvotes")
        if paper.get("github_stars", 0) > 0:
            stars = paper["github_stars"]
            stars_str = f"{stars/1000:.1f}k" if stars >= 1000 else str(stars)
            stats.append(f"⭐ {stars_str} GitHub stars")
        if stats:
            print(f"   {' | '.join(stats)}")

        # One-line summary
        if paper.get("one_line_summary"):
            print(f"   💡 {paper['one_line_summary']}")

        # Summary or abstract
        if paper.get("ai_summary"):
            summary = paper["ai_summary"][:200] + "..." if len(paper["ai_summary"]) > 200 else paper["ai_summary"]
            print(f"   🤖 {summary}")
        elif paper.get("abstract"):
            abstract = paper["abstract"][:150] + "..." if len(paper["abstract"]) > 150 else paper["abstract"]
            print(f"   📝 {abstract}")

        # Keywords
        if paper.get("ai_keywords"):
            print(f"   🏷️ Keywords: {', '.join(paper['ai_keywords'][:5])}")

        # Links line
        links = []
        if paper.get("arxiv_url"):
            links.append(f"arXiv: {paper['arxiv_url']}")
        if paper.get("pdf_url"):
            links.append(f"PDF: {paper['pdf_url']}")
        if paper.get("hf_url"):
            links.append(f"HF: {paper['hf_url']}")
        if paper.get("github_repo"):
            links.append(f"GitHub: {paper['github_repo']}")
        if links:
            print(f"   🔗 {' | '.join(links)}")

        print()


def display_papers(papers: List[Dict[str, Any]], fmt: str = "text") -> None:
    """Display papers in specified format."""
    if fmt == "json":
        print(json.dumps(papers, indent=2, ensure_ascii=False))
        return

    if fmt == "report":
        display_report(papers)
        return

    for i, paper in enumerate(papers, 1):
        heat_index = calculate_heat_index(paper)
        heat_level = calculate_heat_level(heat_index)
        heat_emoji = "🔥" * heat_level

        print(f"{i}. {paper['title']}")
        print(f"   📊 Heat: {heat_emoji} ({heat_index:.1f}/100)")

        if paper.get("upvotes", 0) > 0:
            print(f"   👍 Upvotes: {paper['upvotes']}")

        if paper.get("github_stars", 0) > 0:
            print(f"   ⭐ GitHub Stars: {paper['github_stars']}  {paper.get('github_repo', '')}")

        if paper.get("authors"):
            authors_str = ", ".join(paper["authors"][:3])
            if len(paper["authors"]) > 3:
                authors_str += f" and {len(paper['authors']) - 3} more"
            print(f"   👥 Authors: {authors_str}")

        if paper.get("published_date"):
            print(f"   📅 Published: {paper['published_date']}")

        if paper.get("one_line_summary"):
            print(f"   💡 一句话摘要: {paper['one_line_summary']}")

        if paper.get("ai_summary"):
            summary = paper["ai_summary"][:200] + "..." if len(paper["ai_summary"]) > 200 else paper["ai_summary"]
            print(f"   🤖 AI Summary: {summary}")
        elif paper.get("abstract"):
            abstract_preview = paper["abstract"][:150] + "..." if len(paper["abstract"]) > 150 else paper["abstract"]
            print(f"   📝 Abstract: {abstract_preview}")

        if paper.get("ai_keywords"):
            print(f"   🏷️  Keywords: {', '.join(paper['ai_keywords'][:5])}")

        print(f"   🔗 arXiv: {paper.get('arxiv_url', 'N/A')}")
        print(f"   📄 PDF: {paper.get('pdf_url', 'N/A')}")

        if paper.get("hf_url"):
            print(f"   🤗 HuggingFace: {paper['hf_url']}")

        print()


def main():
    parser = argparse.ArgumentParser(description="Fetch trending AI/ML papers")
    parser.add_argument("--source", choices=["huggingface", "arxiv", "both"], default="huggingface",
                        help="Paper source (default: huggingface)")
    parser.add_argument("--category", default="cs.AI",
                        help="arXiv category (default: cs.AI)")
    parser.add_argument("--limit", type=int, default=5,
                        help="Number of papers to fetch (default: 5)")
    parser.add_argument("--format", choices=["text", "json", "report"], default="text",
                        help="Output format: text, json, or report (default: text)")
    parser.add_argument("--sort", choices=["heat", "date", "upvotes"], default="heat",
                        help="Sort order (default: heat)")
    parser.add_argument("--with-summary", action="store_true", default=False,
                        help="Generate one-line Chinese summary for each paper using LLM")

    args = parser.parse_args()

    papers = []

    # Primary: HuggingFace JSON API
    if args.source in ["huggingface", "both"]:
        hf_papers = fetch_huggingface_papers(limit=args.limit)
        if hf_papers:
            papers.extend(hf_papers)
        elif args.source == "huggingface":
            # HuggingFace failed as sole source — fallback to arXiv
            print("HuggingFace API failed, falling back to arXiv...", file=sys.stderr)
            papers.extend(fetch_arxiv_papers(category=args.category, limit=args.limit))

    # Secondary: arXiv
    if args.source in ["arxiv", "both"]:
        arxiv_papers = fetch_arxiv_papers(category=args.category, limit=args.limit)
        papers.extend(arxiv_papers)

    if not papers:
        print("No papers found. Check your internet connection or try a different source.", file=sys.stderr)
        sys.exit(1)

    # Calculate heat for all papers
    for paper in papers:
        paper["heat_index"] = calculate_heat_index(paper)
        paper["heat_level"] = calculate_heat_level(paper["heat_index"])

    # Sort
    if args.sort == "heat":
        papers.sort(key=lambda x: x["heat_index"], reverse=True)
    elif args.sort == "date":
        papers.sort(key=lambda x: x.get("published_date", ""), reverse=True)
    elif args.sort == "upvotes":
        papers.sort(key=lambda x: x.get("upvotes", 0), reverse=True)

    papers = papers[:args.limit]

    # Generate one-line summaries if requested
    if args.with_summary:
        for paper in papers:
            summary = generate_one_line_summary(paper.get("abstract", ""))
            if summary:
                paper["one_line_summary"] = summary

    display_papers(papers, fmt=args.format)

    # Print summary to stderr so it doesn't pollute JSON output
    if args.format in ("text", "report"):
        print(f"\n📊 Summary: Found {len(papers)} papers", file=sys.stderr)
        if papers:
            avg_heat = sum(p["heat_index"] for p in papers) / len(papers)
            print(f"   Average Heat Index: {avg_heat:.1f}/100", file=sys.stderr)

            sources = {}
            for p in papers:
                src = p.get("source", "unknown")
                sources[src] = sources.get(src, 0) + 1
            if sources:
                print(f"   Sources: {', '.join(f'{k}: {v}' for k, v in sources.items())}", file=sys.stderr)


if __name__ == "__main__":
    main()
