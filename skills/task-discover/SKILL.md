---
name: task-discover
description: "Discover actionable tasks from conversation history"
version: "1.0.0"
tags: [reflection, tasks, self-evolve]
---

# Task Discover

A reflection skill that analyzes recent conversations to find actionable tasks the user mentioned but hasn't completed.

## Execution Mode

- **Trigger**: SessionScanner detects new sessions since last watermark
- **Notification**: Notifies user via mailbox when new tasks are found
- **LLM Calls**: 1 (task discovery)
- **Entropy**: Bounded — max 3 pending tasks, 7-day auto-expiry, Jaccard dedup

## Implementation

Core logic is in `src/everbot/core/skills/task_discover.py`, invoked by heartbeat as an inline skill task.
