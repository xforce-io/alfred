---
name: memory-review
description: "Periodically consolidate and optimize agent memory through session analysis"
version: "1.0.0"
tags: [reflection, memory, self-evolve]
---

# Memory Review

A reflection skill that automatically reviews recent conversation sessions to:

1. **Supplement**: Detect sessions where important user information was missed during initial extraction, and re-extract memories
2. **Consolidate**: Merge duplicate/overlapping memory entries, deprecate outdated ones, and refine content

## Execution Mode

- **Trigger**: SessionScanner detects new sessions since last watermark
- **Notification**: Silent (no user notification)
- **LLM Calls**: 2 (missed session detection + consolidation analysis)
- **Entropy**: Only reduces — entries_after <= entries_before (enforced by IntegrityError)

## Implementation

Core logic is in `src/everbot/core/skills/memory_review.py`, invoked by heartbeat as an inline skill task.
