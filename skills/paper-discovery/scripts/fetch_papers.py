#!/usr/bin/env python3
"""
Paper Discovery Script
Fetch and analyze trending AI/ML papers from arXiv and HuggingFace
"""

import argparse
import json
import re
import sys
from datetime import datetime
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup


def fetch_huggingface_papers(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch trending papers from HuggingFace Daily Papers"""
    url = "https://huggingface.co/papers"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching HuggingFace papers: {e}")
        return []
    
    soup = BeautifulSoup(response.text, "html.parser")
    papers = []
    
    # Try different selectors for paper cards
    selectors = [
        "article",
        "div[class*='paper']",
        "a[href*='/papers/']",
        "div[class*='card']"
    ]
    
    paper_elements = []
    for selector in selectors:
        elements = soup.select(selector)
        if elements:
            paper_elements.extend(elements)
            if len(paper_elements) >= limit * 2:  # Get extra for filtering
                break
    
    seen_ids = set()
    for element in paper_elements:
        if len(papers) >= limit:
            break
        
        # Find paper link
        link = element if element.name == "a" else element.find("a", href=re.compile(r"/papers/\d+\.\d+"))
        if not link:
            continue
        
        href = link.get("href", "")
        match = re.search(r"/papers/(\d+\.\d+)", href)
        if not match:
            continue
        
        paper_id = match.group(1)
        if paper_id in seen_ids:
            continue
        seen_ids.add(paper_id)
        
        # Extract title
        title = link.get_text(strip=True)
        if not title or len(title) < 10:
            # Try to find title in parent or siblings
            title_elem = element.find("h3") or element.find("h2") or element.find("h1")
            if title_elem:
                title = title_elem.get_text(strip=True)
        
        if not title or len(title) < 5:
            continue  # Skip if title is too short
        
        # Extract upvotes
        upvotes = 0
        # Look for numbers in the element
        text_content = element.get_text()
        numbers = re.findall(r'\b\d+\b', text_content)
        for num in numbers:
            try:
                num_int = int(num)
                if 1 <= num_int <= 1000:  # Reasonable upvote range
                    upvotes = num_int
                    break
            except ValueError:
                pass
        
        papers.append({
            "paper_id": paper_id,
            "title": title,
            "upvotes": upvotes,
            "hf_url": f"https://huggingface.co/papers/{paper_id}",
            "arxiv_url": f"https://arxiv.org/abs/{paper_id}",
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
            "source": "huggingface",
            "fetched_at": datetime.now().isoformat()
        })
    
    # Sort by upvotes
    papers.sort(key=lambda x: x["upvotes"], reverse=True)
    return papers[:limit]


def fetch_arxiv_papers(category: str = "cs.AI", limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch recent papers from arXiv API"""
    # Use arXiv API
    api_url = f"https://export.arxiv.org/api/query?search_query=cat:{category}&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
    
    try:
        response = requests.get(api_url, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching arXiv papers: {e}")
        return []
    
    # Parse Atom feed
    soup = BeautifulSoup(response.text, "xml")
    entries = soup.find_all("entry")
    papers = []
    
    for entry in entries[:limit]:
        # Extract ID
        id_elem = entry.find("id")
        if not id_elem:
            continue
        
        arxiv_id = id_elem.text.split("/abs/")[-1]
        # Remove version number
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        
        # Extract title
        title_elem = entry.find("title")
        title = title_elem.text.strip().replace("\n", " ") if title_elem else ""
        
        # Extract authors
        authors = []
        for author in entry.find_all("author"):
            name_elem = author.find("name")
            if name_elem:
                authors.append(name_elem.text.strip())
        
        # Extract abstract
        abstract_elem = entry.find("summary")
        abstract = abstract_elem.text.strip() if abstract_elem else ""
        
        # Extract published date
        published_elem = entry.find("published")
        published_date = published_elem.text[:10] if published_elem else ""  # YYYY-MM-DD
        
        # Extract categories
        categories = []
        for cat in entry.find_all("category"):
            if cat.get("term"):
                categories.append(cat.get("term"))
        
        papers.append({
            "paper_id": arxiv_id,
            "title": title,
            "authors": authors,
            "abstract": abstract[:500] + "..." if len(abstract) > 500 else abstract,  # Truncate long abstracts
            "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "source": "arxiv",
            "published_date": published_date,
            "category": category,
            "categories": categories,
            "fetched_at": datetime.now().isoformat()
        })
    
    return papers


def calculate_heat_index(paper: Dict[str, Any]) -> float:
    """Calculate paper heat index (0-100)"""
    import math
    
    score = 0.0
    
    # 1. Upvotes contribution (for HuggingFace)
    if "upvotes" in paper and paper["upvotes"] > 0:
        upvote_score = min(60, math.log(paper["upvotes"] + 1) * 15)
        score += upvote_score
    
    # 2. Freshness
    if "published_date" in paper and paper["published_date"]:
        # Calculate days since publication
        try:
            pub_date = datetime.strptime(paper["published_date"], "%Y-%m-%d")
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
        except:
            score += 15
    else:
        # No date info, assume recent
        score += 25
    
    # 3. Source weight
    if paper.get("source") == "huggingface":
        score += 10
    elif paper.get("source") == "arxiv":
        score += 5
    
    return min(100, score)


def calculate_heat_level(heat_index: float) -> int:
    """Calculate heat level (1-5 fire emojis)"""
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


def display_papers(papers: List[Dict[str, Any]], format: str = "text") -> None:
    """Display papers in specified format"""
    if format == "json":
        print(json.dumps(papers, indent=2, ensure_ascii=False))
        return
    
    # Text format
    for i, paper in enumerate(papers, 1):
        heat_index = calculate_heat_index(paper)
        heat_level = calculate_heat_level(heat_index)
        heat_emoji = "🔥" * heat_level
        
        print(f"{i}. {paper['title']}")
        print(f"   📊 Heat: {heat_emoji} ({heat_index:.1f}/100)")
        
        if paper.get("upvotes", 0) > 0:
            print(f"   👍 Upvotes: {paper['upvotes']}")
        
        if paper.get("authors"):
            authors_str = ", ".join(paper["authors"][:3])
            if len(paper["authors"]) > 3:
                authors_str += f" and {len(paper['authors']) - 3} more"
            print(f"   👥 Authors: {authors_str}")
        
        if paper.get("published_date"):
            print(f"   📅 Published: {paper['published_date']}")
        
        if paper.get("abstract"):
            abstract_preview = paper["abstract"][:150] + "..." if len(paper["abstract"]) > 150 else paper["abstract"]
            print(f"   📝 Abstract: {abstract_preview}")
        
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
    parser.add_argument("--format", choices=["text", "json"], default="text",
                       help="Output format (default: text)")
    parser.add_argument("--sort", choices=["heat", "date", "upvotes"], default="heat",
                       help="Sort order (default: heat)")
    
    args = parser.parse_args()
    
    papers = []
    
    if args.source in ["huggingface", "both"]:
        hf_papers = fetch_huggingface_papers(limit=args.limit)
        papers.extend(hf_papers)
    
    if args.source in ["arxiv", "both"]:
        arxiv_papers = fetch_arxiv_papers(category=args.category, limit=args.limit)
        papers.extend(arxiv_papers)
    
    if not papers:
        print("No papers found. Check your internet connection or try a different source.")
        sys.exit(1)
    
    # Calculate heat for all papers
    for paper in papers:
        paper["heat_index"] = calculate_heat_index(paper)
        paper["heat_level"] = calculate_heat_level(paper["heat_index"])
    
    # Sort papers
    if args.sort == "heat":
        papers.sort(key=lambda x: x["heat_index"], reverse=True)
    elif args.sort == "date":
        papers.sort(key=lambda x: x.get("published_date", ""), reverse=True)
    elif args.sort == "upvotes":
        papers.sort(key=lambda x: x.get("upvotes", 0), reverse=True)
    
    # Limit to requested number
    papers = papers[:args.limit]
    
    # Display results
    display_papers(papers, format=args.format)
    
    # Print summary
    if args.format == "text":
        print(f"\n📊 Summary: Found {len(papers)} papers")
        if papers:
            avg_heat = sum(p["heat_index"] for p in papers) / len(papers)
            print(f"   Average Heat Index: {avg_heat:.1f}/100")
            
            sources = {}
            for p in papers:
                src = p.get("source", "unknown")
                sources[src] = sources.get(src, 0) + 1
            
            if sources:
                print(f"   Sources: {', '.join(f'{k}: {v}' for k, v in sources.items())}")


if __name__ == "__main__":
    main()
