---
name: invest
description: Primary investment analysis entrypoint. Use it when the user asks about current investment environment, event-driven probability graph simulation, macro liquidity, China market posture, geopolitical risk, causal transmission, asset-level probabilities, or scenario-based investment views.
---

# Invest

Use this skill as the single top-level investment analysis entrypoint.

This skill is designed for event-driven probability graph simulation rather than raw indicator dumping. It consolidates observable signals from lower-level modules and turns them into explainable transmission paths and asset-level probability views.

## Positioning

- `invest` is the only user-facing investment skill.
- Observable data collection is delegated to lower-level signal modules.
- News-driven risk detection can be delegated to gray-rhino style event sources.
- Output must emphasize uncertainty and must not be framed as investment advice.

## Workflow

1. Start with `inv scan` to refresh observable signals.
2. Identify the active themes yourself. Theme recognition is LLM work.
3. Use `inv node --check` before adding new nodes. Reuse existing nodes when semantics overlap.
4. Use `inv node` to override or add analyst judgment when scan output is incomplete or stale.
5. Use `inv edge` and `inv chain` only when you need to extend the default causal graph.
6. Run `inv infer --top 5 --max-hops 6` before presenting probabilities, especially when you need long transmission chains.
7. Use `inv report` to format the final answer.

## Command Entry

```bash
INV="python $SKILL_DIR/scripts/tools.py"
```

## Core Commands

```bash
$INV scan
$INV scan --modules macro,china,rhino

$INV node --check
$INV node --id geo_risk --state high --confidence 0.75 --reason "Escalation confirmed by multiple sources"

$INV edge --from fed_liquidity --to sofr_level --prob '{"tight->elevated": 0.85}'
$INV chain --path "fed_liquidity -> sofr_level -> northbound_flow -> a_share" --label "Fed tightening to A-share"

$INV infer --top 5 --max-hops 6
$INV report
$INV status
```

## Notes

- `scan` writes observable states only. Analyst states from `node` take precedence.
- The default graph already includes a minimal set of macro, China flow, geopolitics, valuation, and asset nodes.
- `scan` may internally reuse lower-level signal scripts for macro, China market, and risk observations.
- `edge --prob` validates state names and probability range before persisting.
- `chain` validates that every hop exists and rejects cyclic or disconnected paths.
- `infer` can auto-discover simple paths up to `--max-hops` and returns `skipped_chains` for invalid paths instead of silently dropping them.
- `infer` is heuristic forward reasoning. It is intended to be explainable, not a strict Bayesian network.
- Output must clearly state uncertainty and should not be framed as investment advice.
