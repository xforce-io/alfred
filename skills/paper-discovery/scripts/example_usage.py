#!/usr/bin/env python3
"""
Example usage of paper discovery functions
"""

import sys
import os

# Add parent directory to path to import from scripts
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import functions from fetch_papers
from fetch_papers import fetch_huggingface_papers, fetch_arxiv_papers, calculate_heat_index, calculate_heat_level

def example_discover_trending():
    """Example: Discover trending papers from HuggingFace"""
    print("📚 Discovering trending papers from HuggingFace...")
    papers = fetch_huggingface_papers(limit=3)
    
    if not papers:
        print("No papers found.")
        return
    
    print(f"Found {len(papers)} trending papers:\n")
    
    for i, paper in enumerate(papers, 1):
        heat_index = calculate_heat_index(paper)
        heat_level = calculate_heat_level(heat_index)
        heat_emoji = "🔥" * heat_level
        
        print(f"{i}. {paper['title']}")
        print(f"   Heat: {heat_emoji} ({heat_index:.1f}/100)")
        print(f"   Upvotes: {paper.get('upvotes', 'N/A')}")
        print(f"   arXiv: {paper['arxiv_url']}")
        print()

def example_arxiv_category():
    """Example: Fetch papers from arXiv CS.AI category"""
    print("🤖 Fetching recent AI papers from arXiv...")
    papers = fetch_arxiv_papers(category="cs.AI", limit=3)
    
    if not papers:
        print("No papers found.")
        return
    
    print(f"Found {len(papers)} recent AI papers:\n")
    
    for i, paper in enumerate(papers, 1):
        heat_index = calculate_heat_index(paper)
        heat_level = calculate_heat_level(heat_index)
        heat_emoji = "🔥" * heat_level
        
        print(f"{i}. {paper['title']}")
        print(f"   Heat: {heat_emoji} ({heat_index:.1f}/100)")
        print(f"   Authors: {', '.join(paper['authors'][:2])}..." if paper['authors'] else "   Authors: Unknown")
        print(f"   Published: {paper.get('published_date', 'Unknown')}")
        print(f"   arXiv: {paper['arxiv_url']}")
        
        if paper.get('abstract'):
            abstract_preview = paper['abstract'][:100] + "..." if len(paper['abstract']) > 100 else paper['abstract']
            print(f"   Abstract: {abstract_preview}")
        
        print()

def example_combined_sources():
    """Example: Combine papers from multiple sources"""
    print("🔍 Combining papers from multiple sources...")
    
    hf_papers = fetch_huggingface_papers(limit=2)
    arxiv_papers = fetch_arxiv_papers(category="cs.LG", limit=2)
    
    all_papers = hf_papers + arxiv_papers
    
    # Calculate heat for all
    for paper in all_papers:
        paper["heat_index"] = calculate_heat_index(paper)
        paper["heat_level"] = calculate_heat_level(paper["heat_index"])
    
    # Sort by heat
    all_papers.sort(key=lambda x: x["heat_index"], reverse=True)
    
    print(f"Found {len(all_papers)} papers total:\n")
    
    for i, paper in enumerate(all_papers, 1):
        heat_emoji = "🔥" * paper["heat_level"]
        source_icon = "🤗" if paper["source"] == "huggingface" else "📄"
        
        print(f"{i}. {paper['title']}")
        print(f"   {source_icon} Source: {paper['source']}")
        print(f"   📊 Heat: {heat_emoji} ({paper['heat_index']:.1f}/100)")
        
        if paper["source"] == "huggingface":
            print(f"   👍 Upvotes: {paper.get('upvotes', 'N/A')}")
        else:
            print(f"   👥 Authors: {', '.join(paper.get('authors', ['Unknown'])[:1])}...")
        
        print(f"   🔗 Link: {paper.get('arxiv_url', paper.get('hf_url', 'N/A'))}")
        print()

def main():
    """Run all examples"""
    print("=" * 60)
    print("PAPER DISCOVERY SKILL - USAGE EXAMPLES")
    print("=" * 60)
    print()
    
    example_discover_trending()
    
    print("-" * 60)
    example_arxiv_category()
    
    print("-" * 60)
    example_combined_sources()
    
    print("=" * 60)
    print("💡 Quick Command Line Usage:")
    print("  python fetch_papers.py --source huggingface --limit 5")
    print("  python fetch_papers.py --source arxiv --category cs.CV --limit 3")
    print("  python fetch_papers.py --source both --limit 10 --format json")
    print("=" * 60)

if __name__ == "__main__":
    main()
