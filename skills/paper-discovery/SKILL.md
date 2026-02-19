---
name: paper-discovery
description: Discover and analyze trending AI/ML papers from arXiv and HuggingFace
version: "1.0.0"
tags: [papers, research, ai, ml, arxiv, huggingface, analysis]
---

# Paper Discovery Skill

This skill enables you to discover trending AI/ML papers and analyze them using AI models. It provides structured guidance for finding, evaluating, and interpreting academic papers.

## When to Use

- User wants to discover recent AI/ML papers
- Need to analyze or summarize academic papers
- Looking for trending research in specific domains
- Want to understand paper insights without reading the full text

## Paper Sources

### 1. HuggingFace Daily Papers
- **URL**: https://huggingface.co/papers
- **Description**: Daily curated AI/ML papers by the HuggingFace community
- **Features**: Community upvotes, direct links to papers, trending indicators

### 2. arXiv Categories
- **CS.AI**: Artificial Intelligence - https://arxiv.org/list/cs.AI/recent
- **CS.LG**: Machine Learning - https://arxiv.org/list/cs.LG/recent
- **CS.CL**: Computation and Language - https://arxiv.org/list/cs.CL/recent
- **CS.CV**: Computer Vision - https://arxiv.org/list/cs.CV/recent

## Paper Discovery Process

### Step 1: Fetch Papers
Use the following Python script to fetch papers from different sources:

```python
import requests
import re
from datetime import datetime
from typing import List, Dict, Any

def fetch_huggingface_papers(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch trending papers from HuggingFace Daily Papers"""
    import requests
    from bs4 import BeautifulSoup
    
    url = "https://huggingface.co/papers"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    papers = []
    
    # Look for paper cards - this may need adjustment based on HuggingFace's HTML structure
    paper_cards = soup.select("article, div[class*='paper']")
    
    for card in paper_cards[:limit]:
        # Extract paper ID from URL
        link = card.find("a", href=re.compile(r"/papers/\d+\.\d+"))
        if not link:
            continue
            
        href = link.get("href", "")
        match = re.search(r"/papers/(\d+\.\d+)", href)
        if not match:
            continue
            
        paper_id = match.group(1)
        
        # Extract title
        title = link.get_text(strip=True)
        if not title or len(title) < 10:
            title_elem = card.find("h3") or card.find("h2")
            if title_elem:
                title = title_elem.get_text(strip=True)
        
        # Extract upvotes
        upvotes = 0
        upvote_elem = card.find(string=re.compile(r"^\d+$"))
        if upvote_elem:
            try:
                upvotes = int(upvote_elem.strip())
            except ValueError:
                pass
        
        papers.append({
            "paper_id": paper_id,
            "title": title,
            "upvotes": upvotes,
            "hf_url": f"https://huggingface.co/papers/{paper_id}",
            "arxiv_url": f"https://arxiv.org/abs/{paper_id}",
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
            "source": "huggingface"
        })
    
    # Sort by upvotes
    papers.sort(key=lambda x: x["upvotes"], reverse=True)
    return papers

def fetch_arxiv_papers(category: str = "cs.AI", limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch recent papers from arXiv API"""
    import requests
    from bs4 import BeautifulSoup
    
    # Use arXiv API
    api_url = f"https://export.arxiv.org/api/query?search_query=cat:{category}&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
    
    response = requests.get(api_url, timeout=30)
    response.raise_for_status()
    
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
        title = title_elem.text.strip().replace("\\n", " ") if title_elem else ""
        
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
        
        papers.append({
            "paper_id": arxiv_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "source": "arxiv",
            "published_date": published_date,
            "category": category
        })
    
    return papers
```

### Step 2: Analyze Paper Heat

Calculate paper heat index based on:
- **Upvotes** (for HuggingFace): Community interest indicator
- **Freshness**: Recent papers get higher scores
- **Source**: HuggingFace papers are pre-curated

```python
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
        from datetime import datetime
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
```

### Step 3: Generate Paper Insights

Use AI models to analyze papers and generate insights:

```python
def generate_paper_insights(paper_info: Dict[str, Any], model_name: str = "qwen-plus") -> Dict[str, Any]:
    """Generate insights for a paper using AI model"""
    
    # Prepare paper information
    paper_context = f"""
    Paper Title: {paper_info.get('title', 'Unknown')}
    Authors: {', '.join(paper_info.get('authors', [])) if paper_info.get('authors') else 'Unknown'}
    Abstract: {paper_info.get('abstract', 'No abstract available')}
    Source: {paper_info.get('source', 'Unknown')}
    Published: {paper_info.get('published_date', 'Unknown')}
    """
    
    # Create analysis prompt
    analysis_prompt = f"""
    Please analyze this academic paper and provide insights in the following structure:
    
    1. **Core Contribution**: What is the main contribution of this paper?
    2. **Key Methods**: What methods or techniques does the paper introduce or use?
    3. **Significance**: Why is this paper important for the field?
    4. **Potential Impact**: What could be the practical applications or implications?
    5. **Limitations**: What are the limitations or areas for improvement?
    6. **One-Sentence Summary**: Summarize the paper in one sentence.
    
    Paper Information:
    {paper_context}
    
    Please provide a structured analysis.
    """
    
    # In a real implementation, you would call an AI model API here
    # For example: response = call_ai_model(model_name, analysis_prompt)
    
    return {
        "paper_id": paper_info.get("paper_id"),
        "title": paper_info.get("title"),
        "analyzed_at": datetime.now().isoformat(),
        "model_name": model_name,
        "insights": {
            "core_contribution": "AI-generated analysis would appear here",
            "key_methods": "AI-generated analysis would appear here",
            "significance": "AI-generated analysis would appear here",
            "potential_impact": "AI-generated analysis would appear here",
            "limitations": "AI-generated analysis would appear here"
        },
        "one_sentence_summary": "AI-generated summary would appear here"
    }
```

## Usage Examples

### Example 1: Discover Trending Papers

```python
# Fetch trending papers from HuggingFace
papers = fetch_huggingface_papers(limit=5)

# Calculate heat for each paper
for paper in papers:
    paper["heat_index"] = calculate_heat_index(paper)
    paper["heat_level"] = calculate_heat_level(paper["heat_index"])

# Sort by heat
papers.sort(key=lambda x: x["heat_index"], reverse=True)

# Display results
for i, paper in enumerate(papers, 1):
    heat_emoji = "🔥" * paper["heat_level"]
    print(f"{i}. {paper['title']}")
    print(f"   Heat: {heat_emoji} ({paper['heat_index']:.1f}/100)")
    print(f"   Upvotes: {paper.get('upvotes', 'N/A')}")
    print(f"   arXiv: {paper['arxiv_url']}")
    print()
```

### Example 2: Analyze Specific Paper

```python
# Get paper by arXiv ID
arxiv_id = "2512.24880"
paper_info = {
    "paper_id": arxiv_id,
    "title": "Example Paper Title",
    "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
    "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
    "source": "arxiv"
}

# Generate insights
insights = generate_paper_insights(paper_info, model_name="qwen-plus")
print(f"Insights for: {insights['title']}")
print(f"One-sentence summary: {insights['one_sentence_summary']}")
print("\nDetailed Analysis:")
for key, value in insights["insights"].items():
    print(f"- {key.replace('_', ' ').title()}: {value}")
```

## Best Practices

1. **Source Diversity**: Check multiple sources (HuggingFace + arXiv categories)
2. **Heat Calculation**: Consider both popularity and freshness
3. **Abstract Focus**: When analyzing, focus on the abstract first
4. **Model Selection**: Use appropriate AI models for analysis
5. **Caching**: Cache results to avoid repeated API calls
6. **Error Handling**: Handle network errors and rate limits gracefully

## Limitations

1. **API Dependencies**: Requires network access and working APIs
2. **HTML Parsing**: HuggingFace website structure may change
3. **AI Model Costs**: Generating insights may incur API costs
4. **Language Focus**: Primarily focused on English papers
5. **Field Coverage**: Mainly AI/ML, other fields may need different sources

## Integration with Existing Systems

This skill can be integrated with:
- **Alfred's paper_insight_api.py** for backend API support
- **AI model services** (OpenAI, Anthropic, Qwen, etc.) for analysis
- **Database systems** for caching and history
- **Notification systems** for paper alerts

## Quick Start Commands

To use this skill immediately:

```python
# Load the skill
_load_resource_skill("paper-discovery")

# Fetch trending papers
papers = fetch_huggingface_papers(limit=5)
for paper in papers:
    print(f"- {paper['title']} (🔥{paper.get('upvotes', 0)})")
```

## Related Skills

- **web-research**: General web research methodology
- **academic-search**: Academic database searching
- **pdf-analysis**: PDF content extraction and analysis
