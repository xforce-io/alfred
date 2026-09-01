---
name: papers
description: Discover, search, and deep-read papers via the researcher CLI (HuggingFace Daily Papers, arXiv, Library evidence cards). Use for 推论文, paper names, arXiv IDs, and 详细说说.
version: "1.0.0"
tags: [papers, research, arxiv, huggingface, library]
---

# Papers

Call the `researcher` CLI. Do **not** curl/wget PDFs, scrape HTML, write inline Python, or call paper-discovery scripts.

**demo_agent host:** this skill has no `scripts/`. The binary is `researcher` on PATH. Daily digest is `trending`; deep dive is `read` (Library evidence card, can take up to 15 minutes — not the 300s heartbeat job).

## When to Use

- Daily paper digest / 热榜 / 「推论文」
- User names a paper (e.g. "看看 SkillCraft")
- User has an arXiv ID
- User wants a deep read (「这篇详细说说」)

## Commands

JSON is the agent contract. Errors go to stderr; stdout is payload only.

```bash
researcher papers trending --format json --limit 10
researcher papers trending --format report --limit 10
researcher papers search "SkillCraft" --format json
researcher papers show 2401.12345 --format json
researcher papers read 2401.12345
```

| Command | Needs workspace? | Writes Library? |
|---|---|---|
| `trending` / `search` / `show` | no | no |
| `read` | default workspace | yes (evidence card) |

Default workspace: `--workspace`, else `RESEARCHER_WORKSPACE_ROOT`, else `workspace:` in `~/.researcher/config.yaml` (absolute super-repo path with `researcher.workspace.yml`).

### Flags

| Option | Values | Default | Where |
|---|---|---|---|
| `--limit` | integer | `10` trending / `5` search | trending, search |
| `--format` | `json`, `report` | `json` | trending, search, show |
| `--source` | `huggingface`, `arxiv`, `both` | `huggingface` | trending |
| `--category` | arXiv category | `cs.AI` | trending |
| `--workspace` | absolute path | config/env | read |

`--format report` is human digest text. Prefer `--format json` then write any product/落地 commentary yourself. The CLI has no `--with-analysis` or `--with-summary`.

## JSON fields

Each item includes: `id` (`arxiv:YYMM.NNNNN`), `paper_id`, `title`, `authors`, `abstract`, `arxiv_url`, `pdf_url`, `source`, `published_date`, `heat_index`, `heat_level`. HuggingFace extras when present: `upvotes`, `hf_url`, `github_repo`, `github_stars`, `ai_summary`, `ai_keywords`.

## Daily digest (heartbeat)

```bash
researcher papers trending --format json --limit 10
```

Then push to the user. For each paper include:

- Title with heat emoji (`🔥` × `heat_level`)
- Upvotes / GitHub stars when present
- Abstract excerpt or `ai_summary` (do not omit)
- Agent-side interpretation from the JSON (CLI does not do this):
  - 🔍 **核心创新点**
  - 🛠️ **可复用技术点**（能否接到 kweaver-core）
  - ⚡ **落地价值评估**
- 分类：`[前沿跟踪]` / `[可落地参考]` / `[竞品分析]`
- arXiv / PDF links

Do **not** reduce papers to a title + heat table. Do not run this job as `papers read` (too slow for the 300s heartbeat).

## Deep read (chat only)

```bash
researcher papers read <arxiv-id>
```

Stdout is the Library evidence card (Essence / Claims / … / Takeaway). If a completed card exists, the CLI reprints it. Do not paraphrase the abstract and call it a deep dive.

## Errors

Non-zero exit: no hits, all sources failed, missing default workspace (`read`), or deep-read failure. Read stderr and tell the user. Do not retry by curling arXiv yourself.

If `researcher papers` is not a command, `researcher` on PATH is too old — say so; do not fall back to paper-discovery.
