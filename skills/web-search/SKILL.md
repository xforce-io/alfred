---
name: web-search
description: Multi-backend web search for real-time information, with fallback, normalized output, and optional page extraction.
version: "2.0.0"
tags: [search, web, news, research, realtime]
---

# Web Search

Search real-time web information through a unified CLI. The skill supports multiple backends, normalized output, backend fallback, and optional page extraction for top results.

## When to Use

- The user asks for latest news, prices, product changes, public company updates, or other time-sensitive facts
- The agent needs quick web search without opening a browser
- The task benefits from fallback across multiple search providers
- The agent needs lightweight page extraction after search

## Scripts

All scripts are located under `$SKILL_DIR/scripts/`.

### Main Entry

```bash
$D = python $SKILL_DIR/scripts/search.py

# Auto-select backend and return text output
$D "OpenAI latest news"

# Force DDGS news search
$D "Fed rate decision" --backend ddgs --type news --timelimit d

# Use Tavily and extract page summaries for top 2 results
$D "Python packaging PEP 723" --backend tavily --extract --extract-top 2 --output json

# Keep fallback enabled in auto mode
$D "TSLA delivery latest" --backend auto --max-results 8

# Disable fallback for strict debugging
$D "TSLA delivery latest" --backend tavily --no-fallback --output json
```

## Backends

| Backend | Needs API Key | Best For |
|---------|---------------|----------|
| `ddgs` | No | Free fallback, lightweight search |
| `tavily` | `TAVILY_API_KEY` | Agent-oriented search and research |
| `auto` | Optional | Picks the best available backend order automatically |

## Output Contract

The script normalizes results to a stable schema:

```json
{
  "ok": true,
  "query": "example",
  "search_type": "text",
  "backend": "tavily",
  "attempted_backends": ["tavily"],
  "count": 2,
  "results": [
    {
      "title": "Example title",
      "url": "https://example.com",
      "snippet": "Example snippet",
      "source": "Example",
      "published": "2026-03-06T00:00:00Z",
      "backend": "tavily",
      "search_type": "text",
      "rank": 1,
      "extracted_text": null
    }
  ]
}
```

## Common Arguments

| Argument | Description |
|----------|-------------|
| `--backend` | `auto`, `ddgs`, `tavily` |
| `--type` | `text` or `news` |
| `--max-results` | Maximum result count |
| `--region` | Region hint for supported backends |
| `--timelimit` | `d`, `w`, `m`, `y` |
| `--extract` | Extract readable text from top result pages |
| `--extract-top` | Number of results to extract |
| `--output` | `text` or `json` |
| `--no-fallback` | Disable backend fallback |

## Environment Variables

```bash
export TAVILY_API_KEY="..."
```

## Dependencies

```bash
pip install requests beautifulsoup4 ddgs
```

`ddgs` is optional when `ddgs` backend is not used.

## Notes

- `auto` backend order is dynamic and prefers configured API-backed providers before free fallback.
- For news-like queries, prefer `--type news` and a narrow `--timelimit`.
- If `--extract` is enabled, readable page text is fetched with a simple HTML extraction path.
