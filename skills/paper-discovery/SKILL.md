---
name: paper-discovery
description: Discover and analyze trending AI/ML papers from HuggingFace and arXiv
version: "2.0.0"
tags: [papers, research, ai, ml, arxiv, huggingface, analysis]
---

# Paper Discovery Skill

Discover trending AI/ML papers and generate structured analysis reports. Also use this skill when a user asks about a specific paper from a previous push (e.g. "这篇详细说说", "深度解读", "原文") — use --paper-id to fetch full details.

## When to Use

- User wants to discover recent AI/ML papers
- **User asks about a specific paper from a previous push** (follow-up questions, deep dive requests)
- Need to analyze or summarize academic papers
- Looking for trending research in specific domains
- Scheduled daily paper digest routines

## IMPORTANT: Always Use the CLI Script

**Do NOT write inline Python code to fetch or parse papers.** Always use the provided CLI script:

```
python skills/paper-discovery/scripts/fetch_papers.py [options]
```

The script uses HuggingFace's official JSON API (with arXiv fallback) and handles all fetching, parsing, error handling, and formatting.

## CLI Usage

### Fetch papers as JSON (for programmatic use)

```bash
python skills/paper-discovery/scripts/fetch_papers.py --source huggingface --limit 10 --format json
```

### Fetch from both sources

```bash
python skills/paper-discovery/scripts/fetch_papers.py --source both --limit 5 --format json
```

### Fetch with specific arXiv category

```bash
python skills/paper-discovery/scripts/fetch_papers.py --source arxiv --category cs.CL --limit 5 --format json
```

### Formatted report output (daily digest)

```bash
python skills/paper-discovery/scripts/fetch_papers.py --source both --limit 5 --format report
```

### With one-line Chinese summary (requires LLM)

```bash
python skills/paper-discovery/scripts/fetch_papers.py --source both --limit 5 --format report --with-summary
```

### Fetch a single paper by arXiv ID

```bash
python skills/paper-discovery/scripts/fetch_papers.py --paper-id 2501.12345 --format json
```

This fetches full metadata (including complete abstract) for a specific paper. Use this when a user asks about a particular paper from a previous push.

### Human-readable output

```bash
python skills/paper-discovery/scripts/fetch_papers.py --source huggingface --limit 5
```

### CLI Options

| Option       | Values                            | Default       | Description                |
|-------------|-----------------------------------|---------------|----------------------------|
| `--source`  | `huggingface`, `arxiv`, `both`    | `huggingface` | Paper source               |
| `--limit`   | integer                           | `5`           | Number of papers           |
| `--format`  | `text`, `json`, `report`          | `text`        | Output format              |
| `--sort`    | `heat`, `date`, `upvotes`         | `heat`        | Sort order                 |
| `--category`| arXiv category string             | `cs.AI`       | arXiv category (arXiv only)|
| `--paper-id`| arXiv ID string                   | none          | Fetch single paper by ID   |
| `--with-summary`| flag                          | off           | Generate one-line Chinese summary via LLM |

## JSON Output Fields

Each paper in JSON output includes:

| Field            | Source       | Description                         |
|-----------------|-------------|-------------------------------------|
| `paper_id`      | both        | arXiv paper ID                      |
| `title`         | both        | Paper title                         |
| `authors`       | both        | List of author names                |
| `abstract`      | both        | Paper abstract (truncated to 500c)  |
| `upvotes`       | huggingface | Community upvotes                   |
| `ai_summary`    | huggingface | AI-generated summary                |
| `ai_keywords`   | huggingface | AI-generated keywords               |
| `github_repo`   | huggingface | GitHub repository URL               |
| `github_stars`  | huggingface | GitHub stars count                  |
| `one_line_summary`| both (opt) | LLM-generated one-line Chinese summary (requires `--with-summary`) |
| `heat_index`    | both        | Computed heat score (0-100)         |
| `heat_level`    | both        | Heat level (1-5)                    |
| `published_date`| both        | Publication date (YYYY-MM-DD)       |
| `arxiv_url`     | both        | arXiv abstract URL                  |
| `pdf_url`       | both        | arXiv PDF URL                       |
| `hf_url`        | huggingface | HuggingFace paper page URL          |

## Paper Discovery Process

### Step 1: Fetch Papers via CLI

Use `_bash()` to call the script and capture JSON output:

```
_bash("python skills/paper-discovery/scripts/fetch_papers.py --source both --limit 10 --format json")
```

### Step 2: Parse and Analyze

Parse the JSON output and generate a structured report. The data already includes `heat_index`, `heat_level`, `ai_summary`, `ai_keywords`, and `github_stars` — use these directly instead of re-computing.

### Step 3: Format Report

Format the report for the user with:
- Paper title with heat emoji (🔥 × heat_level)
- Upvotes, GitHub stars (if available)
- AI summary or abstract excerpt
- Keywords
- Links (arXiv, PDF, HuggingFace, GitHub)

## Example Report Format

```
📚 今日 AI 论文热榜 (2025-01-15)

1. 🔥🔥🔥🔥🔥 Paper Title Here
   👍 142 upvotes | ⭐ 1.2k GitHub stars
   💡 提出了一种新型多模态推理框架，显著提升了复杂任务的准确率
   🤖 AI-generated summary of the paper...
   🏷️ Keywords: LLM, reasoning, benchmark
   🔗 arXiv | PDF | HuggingFace | GitHub

2. 🔥🔥🔥🔥 Another Paper Title
   ...
```

## Responding to Follow-up Questions About a Paper

When a user asks about a specific paper from a previous push (e.g. "memo 这篇", "第二篇论文详细说说"):

1. **Use `--paper-id` to fetch full details**: Extract the paper_id from the previous push data and run:
   ```
   _bash("python skills/paper-discovery/scripts/fetch_papers.py --paper-id <arxiv_id> --format json")
   ```
2. **Present the full abstract** and any available ai_summary, ai_keywords, github_repo
3. **Provide direct links** (arXiv, PDF, HuggingFace)
4. **Do NOT say you cannot access the internet** — you have _bash and can fetch paper metadata via the script

## Push Format Requirements

When pushing paper digests (daily routine or on-demand), you MUST include for each paper:
- Title with heat emoji
- Upvotes / GitHub stars
- **Abstract excerpt or AI summary** (at least 1-2 sentences — do NOT omit this)
- **arXiv/PDF links** (do NOT omit these)
- Keywords if available

**Do NOT reduce papers to just a title + heat score in a table.** The abstract and links are critical — without them, users cannot follow up on papers they're interested in.

## Error Handling

- If HuggingFace API fails, the script automatically falls back to arXiv
- Errors are written to stderr; stdout contains only the data output
- Non-zero exit code means no papers were found from any source

## Best Practices

1. **Always use `--format json`** for programmatic processing
2. **Use `--source both`** for comprehensive discovery
3. **Prefer `ai_summary`** over `abstract` when available (richer context)
4. **Check `github_repo`** to highlight papers with open-source implementations
