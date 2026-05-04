---
name: recall-memory
description: Search agent memory (profile + events) by keyword via BM25-lite. Useful for retrieving past decisions, todos, or user preferences when they fall outside the prompt-injected memory block.
version: "1.0.0"
tags: [memory, recall, search]
---

# Recall Memory

Keyword-search the agent's memory store. The system already injects high-scoring profile entries and recent events into every prompt, but uses this skill when:

- The user references something outside the default time window (e.g. "我们三个月前讨论的方案")
- The injected event block hit its top-k cap and the relevant entry was bumped out
- You need to verify whether a fact was previously recorded before claiming it from scratch
- The user asks "你还记得 X 吗" / "之前是不是说过 X"

Two memory layers can be searched independently or together:

- **profile** — long-lived user portrait (preferences, facts, workflows, decisions)
- **event** — time-anchored occurrences (decisions, todos, incidents, milestones)

## When To Use

- User asks for past decisions, deadlines, or specific details
- You suspect a relevant memory exists but it isn't in the current prompt
- Before drafting a plan, check whether the user has already specified constraints

## When NOT To Use

- If the answer is already visible in the system prompt's `# 历史记忆` / `# 近期事件` section, just use it directly
- For real-time conversation context (use the conversation history)
- For broad summaries — recall is keyword-based, not topic-modeling

## CLI Usage

```bash
python skills/recall-memory/scripts/recall_cli.py \
  --workspace "$WORKSPACE_ROOT" \
  --query "<keywords>" \
  [--kind profile|event|both] \
  [--top-k 5] \
  [--days 30]
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--workspace` | required | Agent workspace root (where `MEMORY.md` lives) |
| `--query` | required | Search keywords or short phrase |
| `--kind` | `both` | Which layer(s) to search: `profile` / `event` / `both` |
| `--top-k` | `5` | Maximum results to return |
| `--days` | `null` | For events, restrict to past N days (omit = search all) |

### Output Schema

```json
{
  "ok": true,
  "data": [
    {
      "id": "abc123",
      "kind": "event",
      "category": "decision",
      "content": "用户决定切到 deepseek-chat",
      "score": 0.8,
      "event_at": "2026-05-01T10:30:00+00:00",
      "due_at": null,
      "rank_score": 4.7,
      "...other MemoryEntry fields..."
    }
  ]
}
```

`rank_score` is the BM25 relevance score. `score` is the memory's own importance score (decayed for events).

On error: `{"ok": false, "error": "..."}`.

## Examples

### Search all layers for a keyword

```bash
python skills/recall-memory/scripts/recall_cli.py \
  --workspace ~/.alfred/agents/demo_agent \
  --query "deepseek"
```

### Search only events from the last 90 days

```bash
python skills/recall-memory/scripts/recall_cli.py \
  --workspace ~/.alfred/agents/demo_agent \
  --query "周五交付" --kind event --days 90
```

### Profile-only lookup

```bash
python skills/recall-memory/scripts/recall_cli.py \
  --workspace ~/.alfred/agents/demo_agent \
  --query "代码风格" --kind profile
```

## Notes

- BM25 is keyword-based — it won't catch synonyms or paraphrases. If the first search fails, try alternative wording.
- Chinese content is tokenized character-by-character; English/code is tokenized by word.
- Results include the entry's full payload (id, kind, content, scores, timestamps) so the caller can decide what to surface to the user.
