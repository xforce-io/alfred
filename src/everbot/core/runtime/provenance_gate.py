"""#127 L1 provenance gate (observe-first).

Assess whether a completed report run is *backed*: did the agent actually run
real data tools, and did it declare cite edges for its claims? This catches F2
("不跑就编" — a report whose numbers came from no tool at all) and measures L2
cite coverage.

**observe-first**: this module only *computes* a verdict from a run's events;
it does NOT alter delivery. The caller logs the verdict so we can measure the
real false-positive rate on live traffic before any banner / hard block is
turned on (see issue #127). Reading the events is pure alfred-side (the milkie
run JSONL) — no milkie change.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Tools that produce/cite/think rather than *fetch data* — excluded from the
# data-tool count so "did real data get fetched?" isn't satisfied by cite/think.
_NON_DATA_TOOLS = frozenset({
    "cite", "declare_relation", "think", "create_plan", "update_step",
    "get_lineage", "get_execution", "get_run_io",
})


@dataclass
class BackingVerdict:
    """How well a report run is backed by tools + provenance."""
    tool_calls: int = 0          # real data-tool executions (excl. cite/think/…)
    cites: int = 0               # `cites` relation edges declared
    commands: List[str] = field(default_factory=list)  # shell commands run

    @property
    def has_tool_backing(self) -> bool:
        return self.tool_calls > 0

    @property
    def has_cites(self) -> bool:
        return self.cites > 0

    def ran_command_matching(self, needle: str) -> bool:
        """True if any shell command contains ``needle`` (e.g. a must_run script)."""
        return any(needle in c for c in self.commands)


def assess_report_backing(events: List[Dict[str, Any]]) -> BackingVerdict:
    """Fold a run's events into a backing verdict. Pure, no IO."""
    v = BackingVerdict()
    for e in events:
        etype = e.get("type")
        payload = e.get("payload") or {}
        if etype == "tool.requested":
            name = payload.get("toolName") or payload.get("name") or ""
            if name and name not in _NON_DATA_TOOLS:
                v.tool_calls += 1
                inp = payload.get("input")
                if isinstance(inp, dict):
                    cmd = inp.get("command")
                    if cmd:
                        v.commands.append(str(cmd))
        elif etype == "relation.created":
            if payload.get("type") == "cites":
                v.cites += 1
    return v


def read_run_events(data_dir: Any, run_id: str) -> List[Dict[str, Any]]:
    """Read a milkie run's JSONL events. Returns [] if missing/unreadable —
    observe-only must never raise into the delivery path."""
    try:
        path = Path(data_dir) / "runs" / f"{run_id}.jsonl"
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except OSError:
        return []
