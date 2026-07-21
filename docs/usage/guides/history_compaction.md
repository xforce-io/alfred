# Session History Compaction

Long-lived channel/primary sessions can accumulate a large chat history base.
Before each chat turn's first LLM request, EverBot may compact that history so
input tokens stay within budget without dropping recent tool chains or critical
user constraints.

## When it runs

- **Trigger**: estimated chat history tokens (heartbeat/placeholder excluded)
  exceed `trigger_tokens`.
- **Path**: chat turn entry (`core_service` pre-turn) and session save mirrors.
- **Short sessions**: no-op; no extra LLM call when under threshold.

## Defaults

| Key | Default | Notes |
|-----|---------|--------|
| `enabled` | `true` | Set `false` for emergency rollback |
| `trigger_tokens` | `40000` | Start compaction above this estimate |
| `target_recent_tokens` | `20000` | Keep recent verbatim window under this |
| `max_summary_tokens` | `2000` | Cap on injected summary body |

Token estimate uses the existing `chars // 3` heuristic (same as save-path compact).

## Configuration

Priority: **agent > global > default**.

```yaml
everbot:
  session:
    history_compaction:
      enabled: true
      trigger_tokens: 40000
      target_recent_tokens: 20000
      max_summary_tokens: 2000
  agents:
    demo_agent:
      session:
        history_compaction:
          trigger_tokens: 30000   # optional per-agent override
```

### Valid ranges

- `trigger_tokens` ≥ 1000  
- `target_recent_tokens` ≥ 500 and ≤ `trigger_tokens`  
- `max_summary_tokens` in `[200, 8000]`  

Invalid values log a warning and fall back to defaults (never crash the chat).

## What happens on trigger

1. Older messages (outside the safe recent window) are summarized with a fast LLM.
2. A single summary marker pair is injected; previous summary text is merged in.
3. The recent window cut is **tool-chain safe** (no orphan `tool` messages).
4. Compacted history is applied to the live provider (Milkie: export → rewrite →
   `/session/import`) and mirrored into the alfred session file.
5. A timeline event `history_compaction` is recorded (sizes + outcome only;
   **no** history body).

### Outcomes

| `outcome` | Meaning |
|-----------|---------|
| `skipped` | Disabled or under trigger |
| `summarized` | Summary + recent window applied |
| `window_trimmed` | Summary failed; structure-safe trim only |
| `over_budget_unavoidable` | Minimal safe keep still over target (e.g. huge single message) |
| `kept_original` | Could not safely reduce; original kept |
| `apply_failed` | Provider import failed; chat continues with original live history |

## Disable risk

Setting `enabled: false` restores the full history base on every turn. Long
sessions can again approach ~90k+ first-round input tokens and are more likely
to hit foreground turn timeouts. Use only for emergency diagnosis.

## Observability

Timeline / logs (English log lines):

```text
history_compaction session=... outcome=summarized before_tokens=... after_tokens=...
```

Event fields: `type`, `provider`, `reason`, `before_tokens`, `after_tokens`,
`summary_tokens`, `retained_messages`, `outcome`, optional `session_id`.

## Related

- Issue #166 — long session history compaction  
- Design: `design.md` / `docs/design/166-long-session-history-compaction.md` (when promoted)  
- Orthogonal: tool result projection (#167), soft timeout (#168)
