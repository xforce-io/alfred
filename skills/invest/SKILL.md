---
name: invest
description: Unified investment environment analysis skill. Use it when the user asks about current investment environment, macro liquidity, China market posture, geopolitical risk, causal transmission, asset-level probabilities, or scenario-based investment views.
---

# Invest

Use this skill when the user wants an investment environment readout, not just a raw data dump.

## Workflow

1. Start with `inv scan` to refresh observable signals.
2. Identify the active themes yourself. Theme recognition is LLM work.
3. Use `inv node --check` before adding new nodes. Reuse existing nodes when semantics overlap.
4. Use `inv node` to override or add analyst judgment when scan output is incomplete or stale.
5. Use `inv edge` and `inv chain` only when you need to extend the default causal graph.
6. Run `inv infer --top 5` before presenting probabilities.
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

$INV infer --top 5
$INV report
$INV status
```

## Notes

- `scan` writes observable states only. Analyst states from `node` take precedence.
- The default graph already includes a minimal set of macro, China flow, geopolitics, valuation, and asset nodes.
- `infer` is heuristic forward reasoning. It is intended to be explainable, not a strict Bayesian network.
- Output must clearly state uncertainty and should not be framed as investment advice.
